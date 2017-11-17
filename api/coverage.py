import logging
from lxml import etree
from nose.tools import set_trace
from config import (
    Configuration,
    CannotLoadConfiguration,
)
from StringIO import StringIO
from core.coverage import (
    CoverageFailure,
    CollectionCoverageProvider,
    WorkCoverageProvider,
)
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import contains_eager
from core.model import (
    Collection,
    ConfigurationSetting,
    CoverageRecord,
    DataSource,
    Edition,
    ExternalIntegration,
    Identifier,
    LicensePool,
    Session,
    WorkCoverageRecord,
)
from core.util.opds_writer import (
    OPDSFeed
)
from core.opds_import import (
    AccessNotAuthenticated,
    MetadataWranglerOPDSLookup,
    OPDSImporter,
    OPDSXMLParser,
    SimplifiedOPDSLookup,
)
from core.opds import AcquisitionFeed
from core.util.http import (
    RemoteIntegrationException,
)

class CollectionSyncImporter(OPDSImporter):

    # Status codes that are generally considered to indicate failure,
    # but which actually indicate success as far as this OPDSImporter
    # is concerned.
    SUCCESS_STATUS_CODES = []

    @classmethod
    def coveragefailure_from_message(cls, data_source, message):
        """Turn a <simplified:message> tag into a CoverageFailure."""

        # The superclass will parse the Identifier for us and handle
        # cases like invalid URNs.
        failure = OPDSImporter.coveragefailure_from_message(
            cls, data_source, message
        )
        if (not failure 
            or not failure.identifier 
            or message.status_code not in cls.SUCCESS_STATUS_CODES):
            return failure

        # What a normal OPDSImporter would consider a failure,
        # we consider a success. Returning the Identifier instead
        # of the CoverageFailure will make sure this Identifier
        # gets a 'success' CoverageRecord.
        return failure.identifier


class RegistrarImporter(CollectionSyncImporter):
    """We are successful whenever the metadata wrangler puts an identifier
    into the catalog, even if no metadata is immediately available.
    """
    SUCCESS_STATUS_CODES = [201, 202]


class ReaperImporter(CollectionSyncImporter):
    """We are successful if the metadata wrangler acknowledges that an
    identifier has been removed, and also if the identifier wasn't in
    the catalog in the first place.
    """
    SUCCESS_STATUS_CODES = [200, 404]


    
class OPDSImportCoverageProvider(CollectionCoverageProvider):
    """Provide coverage for identifiers by looking them up, in batches,
    using the Simplified lookup protocol.
    """
    DEFAULT_BATCH_SIZE = 25
    OPDS_IMPORTER_CLASS = OPDSImporter
    
    def __init__(self, collection, lookup_client, **kwargs):
        """Constructor.

        :param lookup_client: A SimplifiedOPDSLookup object.
        """
        super(OPDSImportCoverageProvider, self).__init__(collection, **kwargs)
        self.lookup_client = lookup_client

    @classmethod
    def all(cls, _db, **kwargs):
        if cls.PROTOCOL and cls.DATA_SOURCE_NAME:
            qu = Collection.by_protocol(_db, cls.PROTOCOL)
            qu = qu.join(ExternalIntegration.settings).filter(
                ConfigurationSetting.key == Collection.DATA_SOURCE_NAME_SETTING,
                ConfigurationSetting.value == cls.DATA_SOURCE_NAME
            ).order_by(func.random())
            for collection in qu:
                yield cls(collection, **kwargs)
        else:
            for collection in super(OPDSImportCoverageProvider, cls).all(_db, **kwargs):
                yield collection

    def process_batch(self, batch):
        """Perform a Simplified lookup and import the resulting OPDS feed."""
        (imported_editions, pools, works, 
         error_messages_by_id) = self.lookup_and_import_batch(batch)

        results = []
        imported_identifiers = set()
        # We grant coverage if an Edition was created from the operation.
        for edition in imported_editions:
            identifier = edition.primary_identifier
            results.append(identifier)
            imported_identifiers.add(identifier)

        # The operation may also have updated information from a
        # number of LicensePools.
        for pool in pools:
            identifier = pool.identifier
            if identifier in imported_identifiers:
                self.finalize_license_pool(pool)
            else:
                msg = "OPDS import operation imported LicensePool, but no Edition."
                results.append(
                    self.failure(identifier, msg, transient=True)
                )

        # Anything left over is either a CoverageFailure, or an
        # Identifier that used to be a CoverageFailure, indicating a
        # 'failure' that the OPDSImporter in use decided was actually
        # a success.
        for failure_or_identifier in error_messages_by_id.values():
            results.append(failure_or_identifier)
        return results

    def process_item(self, identifier):
        """Handle an individual item (e.g. through ensure_coverage) as a very
        small batch. Not efficient, but it works.
        """
        [result] = self.process_batch([identifier])
        return result

    def finalize_license_pool(self, pool):
        """An OPDS entry was matched with a LicensePool. Do something special
        to mark the occasion.

        By default, nothing happens.
        """
        pass

    def lookup_and_import_batch(self, batch):
        """Look up a batch of identifiers and parse the resulting OPDS feed.

        This method is overridden by MockOPDSImportCoverageProvider.
        """
        # id_mapping maps our local identifiers to identifiers the
        # foreign data source will reocgnize.
        id_mapping = self.create_identifier_mapping(batch)
        if id_mapping:
            foreign_identifiers = id_mapping.keys()
        else:
            foreign_identifiers = batch

        response = self.lookup_client.lookup(foreign_identifiers)

        # import_feed_response takes id_mapping so it can map the
        # foreign identifiers back to their local counterparts.
        return self.import_feed_response(response, id_mapping)

    def create_identifier_mapping(self, batch):
        """Map the internal identifiers used for books to the corresponding
        identifiers used by the lookup client.

        By default, no identifier mapping is needed.
        """
        return None

    def import_feed_response(self, response, id_mapping):
        """Confirms OPDS feed response and imports feed.
        """
        self.lookup_client.check_content_type(response)
        importer = self.OPDS_IMPORTER_CLASS(
            self._db, self.collection,
            identifier_mapping=id_mapping,
            data_source_name=self.data_source.name
        )
        return importer.import_from_feed(response.text)


class BaseMetadataWranglerCoverageProvider(OPDSImportCoverageProvider):
    """Makes sure the metadata wrangler knows about all Identifiers
    licensed to a Collection.

    This has two subclasses: MetadataWranglerCollectionRegistrar
    (which adds Identifiers from a circulation manager's catalog to
    the corresponding catalog on the metadata wrangler) and
    MetadataWranglerCollectionReaper (which removes Identifiers from
    the metadata wrangler catalog once they no longer exist in the
    circulation manager's catalog).
    """

    def __init__(self, collection, lookup_client=None, **kwargs):
        """Since we are processing a specific collection, we must be able to
        get an _authenticated_ metadata wrangler lookup client for the
        collection.
        """
        _db = Session.object_session(collection)
        lookup_client = lookup_client or MetadataWranglerOPDSLookup.from_config(
            _db, collection=collection
        )
        super(MetadataWranglerCoverageProvider, self).__init__(
            collection, lookup_client, **kwargs
        )
        if not self.lookup_client.authenticated:
            raise CannotLoadConfiguration(
                "Authentication for the Library Simplified Metadata Wrangler "
                "is not set up. Without this, there is no way to register "
                "your identifiers with the metadata wrangler."
            )

    def create_identifier_mapping(self, batch):
        """The metadata wrangler can look up ISBNs and Overdrive
        identifiers. All other identifier types need to be mapped to
        ISBNs.
        """
        mapping = dict()
        for identifier in batch:
            if identifier.type in [
                    Identifier.AXIS_360_ID, Identifier.BIBLIOTHECA_ID,
                    Identifier.ONECLICK_ID
            ]:
                for e in identifier.equivalencies:
                    if e.output.type == Identifier.ISBN:
                        mapping[e.output] = identifier
                        break
            else:
                mapping[identifier] = identifier
        return mapping


class MetadataWranglerCollectionRegistrar(BaseMetadataWranglerCoverageProvider):
    """Register all Identifiers licensed to a Collection with the
    metadata wrangler.

    If OPDS metadata is immediately returned, make use of it. Even if
    no metadata is returned for an Identifier, mark it as covered.

    Once it's registered, any future updates to the available metadata
    for a given Identifier will be detected by the
    MetadataWranglerCollectionUpdateMonitor.
    """

    SERVICE_NAME = "Metadata Wrangler Collection Registrar"
    OPERATION = CoverageRecord.IMPORT_OPERATION
    OPDS_IMPORTER_CLASS = RegistrarImporter

    def items_that_need_coverage(self, identifiers=None, **kwargs):
        """Retrieves items from the Collection that are not registered with
        the Metadata Wrangler.
        """

        # Start with all items in this Collection that have not been
        # registered.
        uncovered = super(MetadataWranglerCoverageProvider, self)\
            .items_that_need_coverage(identifiers, **kwargs)
        # Make sure they're actually available through this
        # collection.
        uncovered = uncovered.filter(
            or_(LicensePool.open_access, LicensePool.licenses_owned > 0)
        )

        # Exclude items that have been reaped because we stopped
        # having a license.
        reaper_covered = self._db.query(Identifier)\
            .join(Identifier.coverage_records)\
            .filter(
                CoverageRecord.data_source==self.data_source,
                CoverageRecord.collection_id==self.collection_id,
                CoverageRecord.operation==CoverageRecord.REAP_OPERATION
            )

        # If any items were reaped earlier but have since been
        # relicensed or otherwise added back to the collection, remove
        # their reaper CoverageRecords. This ensures we get Metadata
        # Wrangler coverage for books that have had their licenses
        # repurchased or extended.
        relicensed = reaper_covered.join(Identifier.licensed_through).filter(
                LicensePool.collection_id==self.collection_id,
                or_(LicensePool.licenses_owned > 0, LicensePool.open_access)
            ).options(contains_eager(Identifier.coverage_records))

        needs_commit = False
        for identifier in relicensed.all():
            for record in identifier.coverage_records:
                if (record.data_source==self.data_source and
                    record.collection_id==self.collection_id and
                    record.operation==CoverageRecord.REAP_OPERATION):
                    # Delete any reaper CoverageRecord for this Identifier
                    # in this Collection.
                    self._db.delete(record)
                    needs_commit = True
        if needs_commit:
            self._db.commit()

        # We want all items that don't have a SYNC coverage record, so
        # long as they're also missing a REAP coverage record (uncovered).
        # If they were relicensed, we just removed the REAP coverage
        # record.
        return uncovered.except_(reaper_covered).order_by(Identifier.id)


class MetadataWranglerCollectionReaper(BaseMetadataWranglerCoverageProvider):
    """Removes unlicensed identifiers from the remote Metadata Wrangler
    Collection
    """

    SERVICE_NAME = "Metadata Wrangler Reaper"
    OPERATION = CoverageRecord.REAP_OPERATION
    OPDS_IMPORTER_CLASS = ReaperImporter

    def items_that_need_coverage(self, identifiers=None, **kwargs):
        """Retrieves Identifiers that were synced but are no longer licensed.
        """
        qu = self._db.query(Identifier).select_from(LicensePool).\
            join(LicensePool.identifier).join(CoverageRecord).\
            filter(LicensePool.collection_id==self.collection_id).\
            filter(LicensePool.licenses_owned==0, LicensePool.open_access!=True).\
            filter(CoverageRecord.data_source==self.data_source).\
            filter(CoverageRecord.operation==CoverageRecord.SYNC_OPERATION).\
            filter(CoverageRecord.status==CoverageRecord.SUCCESS).\
            filter(CoverageRecord.collection==self.collection)

        if identifiers:
            qu = qu.filter(Identifier.id.in_([x.id for x in identifiers]))
        return qu

    def finalize_batch(self):
        """Deletes Metadata Wrangler coverage records of reaped Identifiers

        This allows Identifiers to be added to the collection again via
        MetadataWranglerCoverageProvider lookup if a license is repurchased.
        """
        qu = self._db.query(Identifier.id).join(Identifier.coverage_records)
        reaper_covered = qu.filter(
            CoverageRecord.data_source==self.data_source,
            CoverageRecord.operation==CoverageRecord.REAP_OPERATION
        )
        wrangler_covered = qu.filter(
            CoverageRecord.data_source==self.data_source,
            CoverageRecord.operation==CoverageRecord.SYNC_OPERATION
        )
        # Get the db ids of identifiers that have been both synced and reaped.
        subquery = reaper_covered.intersect(wrangler_covered).subquery()

        # Retrieve the outdated syncing coverage record and delete it.
        coverage_records = self._db.query(CoverageRecord).\
                join(CoverageRecord.identifier).\
                join(subquery, Identifier.id.in_(subquery)).\
                filter(
                    CoverageRecord.data_source==self.data_source,
                    CoverageRecord.operation==CoverageRecord.SYNC_OPERATION
                )
        for record in coverage_records.all():
            self._db.delete(record)
        super(MetadataWranglerCollectionReaper, self).finalize_batch()


class MetadataUploadCoverageProvider(BaseMetadataWranglerCoverageProvider):
    """Provide coverage for identifiers by uploading OPDS metadata to
    the metadata wrangler.
    """
    DEFAULT_BATCH_SIZE = 25
    SERVICE_NAME = "Metadata Upload Coverage Provider"
    OPERATION = CoverageRecord.METADATA_UPLOAD_OPERATION
    DATA_SOURCE_NAME = DataSource.INTERNAL_PROCESSING
       
    def items_that_need_coverage(self, identifiers=None, **kwargs):
        """Find all identifiers lacking coverage from this CoverageProvider.
        Only identifiers that have CoverageRecords in the 'transient
        failure' state will be returned. Unlike with other
        CoverageProviders, Identifiers that have no CoverageRecord at
        all will not be processed.
        """
        qu = super(MetadataUploadCoverageProvider, self).items_that_need_coverage(
            identifiers=identifiers, **kwargs
        )
        qu = qu.filter(CoverageRecord.id != None)
        return qu

    def process_batch(self, batch):
        """Create an OPDS feed from a batch and upload it to the metadata client."""
        works = []
        results = []
        for identifier in batch:
            work = self.work(identifier)
            if not isinstance(work, CoverageFailure):
                works.append(work)
                results.append(identifier)
            else:
                results.append(work)
        feed = AcquisitionFeed(self._db, "Metadata Upload Feed", "", works, None)
        self.upload_client.add_with_metadata(feed)
        
        # We grant coverage for all identifiers if the upload doesn't raise an exception.
        return results


class ContentServerBibliographicCoverageProvider(OPDSImportCoverageProvider):
    """Make sure our records for open-access books match what the content
    server says.
    """
    SERVICE_NAME = "Open-access content server bibliographic coverage provider"
    DATA_SOURCE_NAME = DataSource.OA_CONTENT_SERVER
    INPUT_IDENTIFIER_TYPES = None

    PROTOCOL = ExternalIntegration.OPDS_IMPORT
    
    def __init__(self, collection, lookup_client, *args, **kwargs):
        if not lookup_client:
            content_server_url = (
                Configuration.integration_url(
                    Configuration.CONTENT_SERVER_INTEGRATION
                )
            )
            lookup_client = SimplifiedOPDSLookup(content_server_url)
        super(ContentServerBibliographicCoverageProvider, self).__init__(
            collection, lookup_client, *args, **kwargs
        )

    def finalize_license_pool(self, license_pool):
        """Ensure that a LicensePool successfully imported from the content
        server has a presentation-ready Work.
        """
        work, new_work = license_pool.calculate_work(even_if_no_author=True)
        work.set_presentation_ready()
        
    def items_that_need_coverage(self, *args, **kwargs):
        """Only identifiers already associated with an open-access LicensePool
        need coverage.
        """
        qu = super(ContentServerBibliographicCoverageProvider, 
                   self).items_that_need_coverage(*args, **kwargs)
        qu = qu.join(Identifier.licensed_through).filter(
            LicensePool.open_access==True
        )
        return qu


class MockOPDSImportCoverageProvider(OPDSImportCoverageProvider):

    SERVICE_NAME = "Mock Provider"
    DATA_SOURCE_NAME = DataSource.OA_CONTENT_SERVER
    
    def __init__(self, collection, *args, **kwargs):
        super(MockOPDSImportCoverageProvider, self).__init__(
            collection, None, *args, **kwargs
        )
        self.batches = []
        self.finalized = []
        self.import_results = []

    def queue_import_results(self, editions, pools, works, messages_by_id):
        self.import_results.insert(0, (editions, pools, works, messages_by_id))

    def finalize_license_pool(self, license_pool):
        self.finalized.append(license_pool)
        super(MockOPDSImportCoverageProvider, self).finalize_license_pool(
            license_pool
        )

    def lookup_and_import_batch(self, batch):
        self.batches.append(batch)
        return self.import_results.pop()
