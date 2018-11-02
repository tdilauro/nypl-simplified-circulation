from nose.tools import set_trace
import flask
from flask import Response
from api.admin.problem_details import *
from api.nyt import NYTBestSellerAPI
from api.novelist import NoveListAPI
from core.opds_import import MetadataWranglerOPDSLookup
from core.model import (
    ExternalIntegration,
    get_one,
)
from core.util.http import HTTP
from core.util.problem_detail import ProblemDetail

from . import SettingsController

class MetadataServicesController(SettingsController):

    def __init__(self, manager):
        super(MetadataServicesController, self).__init__(manager)
        provider_apis = [
                            NYTBestSellerAPI,
                            NoveListAPI,
                            MetadataWranglerOPDSLookup,
                        ]

        self.protocols = self._get_integration_protocols(provider_apis, protocol_name_attr="PROTOCOL")

    def process_metadata_services(self):
        self.require_system_admin()
        if flask.request.method == 'GET':
            return self.process_get()
        else:
            return self.process_post()

    def process_get(self):
        metadata_services = self._get_integration_info(ExternalIntegration.METADATA_GOAL, self.protocols)
        return dict(
            metadata_services=metadata_services,
            protocols=self.protocols,
        )

    def process_post(self, do_get=HTTP.debuggable_get, do_post=HTTP.debuggable_post):
        name = flask.request.form.get("name")
        protocol = flask.request.form.get("protocol")
        fields = {"name": name, "protocol": protocol}
        form_field_error = self.validate_form_fields(**fields)
        if form_field_error:
            return form_field_error

        id = flask.request.form.get("id")
        is_new = False
        if id:
            # Find an existing service in order to edit it
            service = self.look_up_service_by_id(id, protocol)
        else:
            service, is_new = self._create_integration(
                self.protocols, protocol, ExternalIntegration.METADATA_GOAL
            )

        if isinstance(service, ProblemDetail):
            self._db.rollback()
            return service

        name_error = self.check_name_unique(service, name)
        if name_error:
            self._db.rollback()
            return name_error

        protocol_error = self.set_protocols(service, protocol)
        if protocol_error:
            self._db.rollback()
            return protocol_error

        wrangler_error = self.register_with_metadata_wrangler(is_new, service)
        if wrangler_error:
            self._db.rollback()
            return wrangler_error

        service.name = name

        if is_new:
            return Response(unicode(service.id), 201)
        else:
            return Response(unicode(service.id), 200)

    def validate_form_fields(self, **fields):
        """The 'name' and 'protocol' fields cannot be blank, and the protocol must
        be selected from the list of recognized protocols."""

        name = fields.get("name")
        protocol = fields.get("protocol")

        if not name:
            return INCOMPLETE_CONFIGURATION
        if not protocol:
            return NO_PROTOCOL_FOR_NEW_SERVICE
        if protocol not in [p.get("name") for p in self.protocols]:
            return UNKNOWN_PROTOCOL

    def look_up_service_by_id(self, id, protocol):
        """Find an existing service, and make sure that the user is not trying to edit
        its protocol."""

        service = get_one(self._db, ExternalIntegration, id=id, goal=ExternalIntegration.METADATA_GOAL)
        if not service:
            return MISSING_SERVICE
        if protocol != service.protocol:
            return CANNOT_CHANGE_PROTOCOL
        return service

    def check_name_unique(self, new_service, name):
        """A service cannot be created with, or edited to have, the same name
        as a service that already exists."""

        existing_service = get_one(self._db, ExternalIntegration, name=name)
        if existing_service and not existing_service.id == new_service.id:
            # Without checking that the IDs are different, you can't save
            # changes to an existing service unless you've also changed its name.
            return INTEGRATION_NAME_ALREADY_IN_USE

    def set_protocols(self, service, protocol):
        """Validate the protocol that the user has submitted; depending on whether
        the validations pass, either save it to this metadata service or
        return an error message."""

        [protocol] = [p for p in self.protocols if p.get("name") == protocol]
        result = self._set_integration_settings_and_libraries(service, protocol)
        if isinstance(result, ProblemDetail):
            return result

    def register_with_metadata_wrangler(self, is_new, service):
        """Register this site with the Metadata Wrangler."""

        if ((is_new or not service.password) and
            service.protocol == ExternalIntegration.METADATA_WRANGLER):

            problem_detail = self.sitewide_registration(
                service, do_get=do_get, do_post=do_post
            )
            if problem_detail:
                return problem_detail

    def process_delete(self, service_id):
        return self._delete_integration(
            service_id, ExternalIntegration.METADATA_GOAL
        )
