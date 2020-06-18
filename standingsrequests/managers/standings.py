from datetime import timedelta

from django.db import transaction
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from allianceauth.eveonline.models import EveCharacter
from allianceauth.notifications import notify
from allianceauth.authentication.models import CharacterOwnership
from allianceauth.services.hooks import get_extension_logger

from esi.models import Token

from .. import __title__
from ..app_settings import (
    STANDINGS_API_CHARID,
    STR_CORP_IDS,
    STR_ALLIANCE_IDS,
    SR_REQUIRED_SCOPES,
    SR_OPERATION_MODE,
    SR_NOTIFICATIONS_ENABLED,
    SR_PREVIOUSLY_EFFECTIVE_GRACE_HOURS,
)
from ..helpers.esi_fetch import esi_fetch
from ..helpers.evecorporation import EveCorporation
from ..models import (
    AllianceStanding,
    CharacterAssociation,
    ContactSet,
    ContactLabel,
    CorpStanding,
    EveNameCache,
    PilotStanding,
    StandingsRequest,
    StandingsRevocation,
)
from ..utils import chunks, LoggerAddTag

logger = LoggerAddTag(get_extension_logger(__name__), __title__)


class StandingsManager:
    charID = STANDINGS_API_CHARID

    @classmethod
    def token(cls):
        """returns a valid token with required scopes"""
        return (
            Token.objects.filter(character_id=cls.charID)
            .require_scopes(ContactSet.required_esi_scope())
            .require_valid()
            .first()
        )

    @classmethod
    @transaction.atomic
    def api_update_standings(cls):
        """fetches contacts with standings for configured alliance 
        or corporation from ESI and stores them as new ContactSet
        """
        try:
            contacts = ContactsWrapper(cls.token(), cls.charID)
        except Exception:
            logger.exception("APIError occurred while trying to query api server.")
            return

        contacts_set = ContactSet()
        contacts_set.save()
        # Add Labels
        cls.api_add_labels(contacts_set, contacts.allianceLabels)
        # Add Contacts
        cls.api_add_contacts(contacts_set, contacts.alliance)
        return contacts_set

    @classmethod
    def api_add_labels(cls, contact_set, labels):
        """
        Add the list of labels to the given ContactSet
        :param contact_set: ContactSet instance
        :param labels: Label dictionary
        :return:
        """
        for label in labels:
            contact_label = ContactLabel(
                label_id=label.id, name=label.name, contact_set=contact_set
            )
            contact_label.save()

    @classmethod
    def api_add_contacts(cls, contact_set, contacts):
        """
        Add all contacts to the given ContactSet
        Labels _MUST_ be added before adding contacts
        :param contact_set: Django ContactSet to add contacts to
        :param contacts: List of ContactsWrapper.Contact to add
        :return:
        """
        for c in contacts:
            # Flatten labels so we can do a simple in comparison
            flat_labels = [label.id for label in c.labels]
            # Create a list of applicable django ContactLabel objects
            # Can be replaced in django 1.9 as .set() is available
            labels = [
                label
                for label in contact_set.contactlabel_set.all()
                if label.label_id in flat_labels
            ]
            StandingFactory.create_standing(
                contact_set=contact_set,
                contact_type_id=c.type_id,
                contact_id=c.id,
                name=c.name,
                standing=c.standing,
                labels=labels,
            )

    @classmethod
    def pilot_in_organisation(cls, character_id):
        """
        Check if the Pilot is in the auth instances organisation
        :param character_id: str EveCharacter character_id
        :return: bool True if the character is in the organisation, False otherwise
        """
        pilot = EveCharacter.objects.get_character_by_id(character_id)
        if pilot is None:
            return False

        if (
            str(pilot.corporation_id) in STR_CORP_IDS
            or str(pilot.alliance_id) in STR_ALLIANCE_IDS
        ):
            return True

        return False

    @classmethod
    def all_corp_apis_recorded(cls, corp_id, user):
        """
        Checks if a user has all of the required corps APIs recorded for
         standings to be permitted
        :param corp_id: corp to check for
        :param user: User to check for
        :return: True if they can request standings, False if they cannot
        """
        # TODO: Need to figure out how to check if esi keys exists.....
        keys_recorded = sum(
            [
                1
                for a in EveCharacter.objects.filter(
                    character_ownership__user=user
                ).filter(corporation_id=corp_id)
                if StandingsManager.has_required_scopes_for_request(a)
            ]
        )
        corp = EveCorporation.get_corp_by_id(int(corp_id))
        logger.debug(
            "Got %d keys recorded for %d total corp members",
            keys_recorded,
            corp.member_count or None,
        )
        return corp is not None and keys_recorded >= corp.member_count

    @classmethod
    def process_pending_standings(cls, standings_class=None):
        """
        Process StandingsRequests and StandingsRevocations 
        and mark them as effective if standings have been set
        :type standings_class: AbstractStandingsRequest class to process exclusively
        :return: None
        """
        # Skip if a type is specified and this isn't the specified type
        if standings_class is None or standings_class == StandingsRequest:
            logger.debug("Processing StandingsRequests")
            cls.process_requests(StandingsRequest.objects.all())
        else:
            logger.debug("Skipping StandingsRequests")

        if standings_class is None or standings_class == StandingsRevocation:
            logger.debug("Processing StandingsRevocations")
            cls.process_requests(StandingsRevocation.objects.all())
        else:
            logger.debug("Skipping StandingsRevocations")

    @classmethod
    def process_requests(cls, standing_requests: QuerySet) -> None:
        """
        Process all the Standing requests/revocation objects
        :param standing_requests: AbstractStandingsRequest QuerySet        
        """
        organization = ContactSet.standings_source_entity()
        organization_name = organization.name if organization else ""
        for standing_request in standing_requests:
            character_name = EveNameCache.get_name(standing_request.contact_id)
            was_already_effective = standing_request.is_effective
            is_satisfied_standing = standing_request.process_standing()
            if (
                is_satisfied_standing
                and standing_request.contact_type_id in PilotStanding.contact_types
            ):
                if SR_NOTIFICATIONS_ENABLED and not was_already_effective:
                    if type(standing_request) == StandingsRequest:
                        # Request, send a notification
                        notify(
                            user=standing_request.user,
                            title=_(
                                "%s: Standing for %s now in effect"
                                % (__title__, character_name)
                            ),
                            message=_(
                                "'%s' now has blue standing with your "
                                "character '%s' Please also update "
                                "the standing of your character accordingly."
                            )
                            % (organization_name, character_name),
                        )
                    elif type(standing_request) == StandingsRevocation:
                        # Revocation. Try and send a standing_request
                        # (user or character may be deleted)
                        try:
                            character = EveCharacter.objects.get(
                                character_id=standing_request.contact_id
                            )
                        except EveCharacter.DoesNotExist:
                            pass
                        else:
                            if hasattr(character, "userprofile"):
                                user = character.userprofile.user
                                notify(
                                    user=user,
                                    title="%s: Standing for %s revoked"
                                    % (__title__, character_name),
                                    message=_(
                                        "'%s' no longer has blue standing with your "
                                        "character '%s' Please also update "
                                        "the standing of your character accordingly."
                                    )
                                    % (organization_name, character_name),
                                )

            elif is_satisfied_standing:
                # Just catching all other contact types (corps/alliances)
                # that are set effective
                pass

            elif (
                not is_satisfied_standing
                and standing_request.is_effective
                and (
                    standing_request.effective_date
                    < timezone.now() - timedelta(SR_PREVIOUSLY_EFFECTIVE_GRACE_HOURS)
                )
            ):
                # Standing is not effective, but has previously
                # been marked as effective.
                # Unset effective
                logger.info(
                    "Standing for %d is marked as effective but is not "
                    "satisfied in game. Resetting to initial state"
                    % standing_request.contact_id
                )
                standing_request.reset_to_initial()

            else:
                # Check the standing hasn't been set actioned
                # and not updated in game
                actioned_timeout = standing_request.check_standing_actioned_timeout()
                if actioned_timeout is not None and actioned_timeout:
                    logger.info(
                        "Standing request for contact ID %d has timedout "
                        "and will be reset" % standing_request.contact_id
                    )
                    if SR_NOTIFICATIONS_ENABLED:
                        title = _("Standing request reset for %s" % character_name)
                        message = _(
                            "The standings request for character '%s' from user "
                            "'%s' has been reset as it did not appear in "
                            "game before the timeout period expired."
                            % (character_name, standing_request.user)
                        )
                        # Notify standing manager
                        notify(user=actioned_timeout, title=title, message=message)
                        # Notify the user
                        notify(user=standing_request.user, title=title, message=message)

    @classmethod
    def update_character_associations_auth(cls):
        """
        Update all character associations based on auth relationship data
        :return:
        """
        chars = EveCharacter.objects.all()
        for c in chars:
            logger.debug("Updating Association from Auth for %s", c.character_name)
            try:
                ownership = CharacterOwnership.objects.get(character=c)
                main = (
                    ownership.user.profile.main_character.character_id
                    if ownership.user.profile.main_character
                    else None
                )

            except CharacterOwnership.DoesNotExist:
                main = None

            assoc, _ = CharacterAssociation.objects.update_or_create(
                character_id=c.character_id,
                defaults={
                    "corporation_id": c.corporation_id,
                    "main_character_id": main,
                    "alliance_id": c.alliance_id,
                    "updated": timezone.now(),
                },
            )
            EveNameCache.update_name(assoc.character_id, c.character_name)

    @classmethod
    def update_character_associations_api(cls):
        """
        Update all character corp associations we have standings for that 
        aren't being updated locally
        Cache timeout should be longer than update_character_associations_auth 
        update schedule to
        prevent unnecessarily updating characters we already have local data for.
        :return:
        """
        # gather character associations of pilots which meed to be updated
        try:
            all_pilots = ContactSet.objects.latest().pilotstanding_set.values_list(
                "contact_id", flat=True
            )
            expired_character_associations = CharacterAssociation.get_api_expired_items(
                all_pilots
            ).values_list("character_id", flat=True)
            expired_pilots = set(all_pilots).intersection(
                expired_character_associations
            )
            known_pilots = CharacterAssociation.objects.values_list(
                "character_id", flat=True
            )
            unknown_pilots = [
                pilot for pilot in all_pilots if pilot not in known_pilots
            ]
            pilots_to_fetch = list(expired_pilots | set(unknown_pilots))

        except ObjectDoesNotExist:
            logger.warning(
                "No standings set available to update "
                "character associations with. Aborting"
            )
        else:
            # Fetch the data in acceptable sizes from the API
            chunk_size = 1000
            for pilots_chunk in chunks(pilots_to_fetch, chunk_size):
                try:
                    esi_response = esi_fetch(
                        "Character.post_characters_affiliation",
                        args={"characters": pilots_chunk},
                    )
                    for association in esi_response:
                        corporation_id = association["corporation_id"]
                        alliance_id = (
                            association["alliance_id"]
                            if "alliance_id" in association
                            else None
                        )
                        character_id = association["character_id"]
                        CharacterAssociation.objects.update_or_create(
                            character_id=character_id,
                            defaults={
                                "corporation_id": corporation_id,
                                "alliance_id": alliance_id,
                                "updated": timezone.now(),
                            },
                        )

                except Exception:
                    logger.exception(
                        "Could not fetch associations pilots_chunk from ESI"
                    )

    @classmethod
    def validate_standings_requests(cls):
        """
        Validate all StandingsRequests and check 
        that the user requesting them has permission and has API keys
        associated with the character/corp. 
        Invalid standings requests are deleted, which may or may not generate a
        StandingsRevocation depending on their state.
        :return: int The number of deleted requests
        """
        logger.debug("Validating standings requests")
        deleted_count = 0
        for standing_request in StandingsRequest.objects.all():
            logger.debug(
                "Checking request for contact_id %d", standing_request.contact_id
            )
            if not standing_request.user.has_perm(
                "standingsrequests.request_standings"
            ):
                logger.debug("Request is invalid, user does not have permission")
                is_valid = False

            elif CorpStanding.is_corp(
                standing_request.contact_type_id
            ) and not cls.all_corp_apis_recorded(
                standing_request.contact_id, standing_request.user
            ):
                logger.debug("Request is invalid, not all corp API keys recorded.")
                is_valid = False

            else:
                is_valid = True

            if not is_valid:
                logger.info(
                    "Deleting invalid standing request for contact_id %d",
                    standing_request.contact_id,
                )
                standing_request.delete()
                deleted_count += 1

        return deleted_count

    @staticmethod
    def has_required_scopes_for_request(character: EveCharacter) -> bool:
        """returns true if given character has the required scopes 
        for issueing a standings request else false
        """
        try:
            ownership = CharacterOwnership.objects.get(
                character__character_id=character.character_id
            )
        except CharacterOwnership.DoesNotExist:
            return False

        else:
            user = ownership.user
            state_name = user.profile.state.name
            scopes_string = " ".join(
                StandingsManager.get_required_scopes_for_state(state_name)
            )
            has_required_scopes = (
                Token.objects.filter(character_id=character.character_id)
                .require_scopes(scopes_string)
                .require_valid()
                .exists()
            )
            return has_required_scopes

    @staticmethod
    def get_required_scopes_for_state(state_name: str) -> list:
        state_name = "" if not state_name else state_name
        return (
            SR_REQUIRED_SCOPES[state_name]
            if state_name in SR_REQUIRED_SCOPES
            else list()
        )


class StandingFactory:
    @classmethod
    def create_standing(
        cls, contact_set, contact_type_id, contact_id, name, standing, labels
    ):
        StandingType = cls.get_class_for_contact_type(contact_type_id)
        standing = StandingType(
            contact_set=contact_set,
            contact_id=contact_id,
            name=name,
            standing=standing,
        )
        standing.save()
        for label in labels:
            standing.labels.add(label)
        standing.save()
        return standing

    @staticmethod
    def get_class_for_contact_type(contact_type_id):
        if contact_type_id in PilotStanding.contact_types:
            return PilotStanding
        elif contact_type_id in CorpStanding.contact_types:
            return CorpStanding
        elif contact_type_id in AllianceStanding.contact_types:
            return AllianceStanding
        raise NotImplementedError()


class ContactsWrapper:
    """Converts raw contacts and contact labels data from ESI into an object"""

    class Label:
        def __init__(self, json):
            self.id = json["label_id"]
            self.name = json["label_name"]

        def __str__(self):
            return u"{}".format(self.name)

        def __repr__(self):
            return str(self)

    class Contact:
        @staticmethod
        def get_type_id_from_name(type_name):
            """
            Maps new ESI name to old type id.
            Character type is allways mapped to 1373
            And faction type to 500000
            Determines the contact type:
            2 = Corporation
            1373-1386 = Character
            16159 = Alliance
            500001 - 500024 = Faction
            """
            if type_name == "character":
                return 1373
            if type_name == "alliance":
                return 16159
            if type_name == "faction":
                return 500001
            if type_name == "corporation":
                return 2

            raise NotImplementedError("This contact type is not mapped")

        def __init__(self, json, labels, names_info):
            self.id = json["contact_id"]
            # TODO: remove this and translate id to name when displayed

            self.name = (
                names_info[self.id]
                if self.id in names_info
                else "Could not get name from API"
            )

            self.standing = json["standing"]

            self.in_watchlist = json["in_watchlist"] if "in_watchlist" in json else None

            self.label_ids = (
                json["label_ids"]
                if "label_ids" in json and json["label_ids"] is not None
                else []
            )

            self.type_id = self.__class__.get_type_id_from_name(json["contact_type_id"])
            # list of labels
            self.labels = [label for label in labels if label.id in self.label_ids]

        def __str__(self):
            return u"{}".format(self.name)

        def __repr__(self):
            return str(self)

    def __init__(self, token, character_id):
        self.alliance = []
        self.allianceLabels = []

        if SR_OPERATION_MODE == "alliance":
            alliance_id = EveCharacter.objects.get_character_by_id(
                character_id
            ).alliance_id
            labels = esi_fetch(
                "Contacts.get_alliances_alliance_id_contacts_labels",
                args={"alliance_id": alliance_id},
                token=token,
            )
            for label in labels:
                self.allianceLabels.append(self.Label(label))

            contacts = esi_fetch(
                "Contacts.get_alliances_alliance_id_contacts",
                args={"alliance_id": alliance_id},
                token=token,
                has_pages=True,
            )
        elif SR_OPERATION_MODE == "corporation":
            corporation_id = EveCharacter.objects.get_character_by_id(
                character_id
            ).corporation_id
            labels = esi_fetch(
                "Contacts.get_corporations_corporation_id_contacts_labels",
                args={"corporation_id": corporation_id},
                token=token,
            )
            for label in labels:
                self.allianceLabels.append(self.Label(label))

            contacts = esi_fetch(
                "Contacts.get_corporations_corporation_id_contacts",
                args={"corporation_id": corporation_id},
                token=token,
                has_pages=True,
            )
        else:
            raise NotImplementedError()

        logger.debug("Got %d contacts in total", len(contacts))
        entity_ids = []
        for contact in contacts:
            entity_ids.append(contact["contact_id"])

        name_info = EveNameCache.get_names(entity_ids)
        for contact in contacts:
            self.alliance.append(self.Contact(contact, self.allianceLabels, name_info))
