# encoding: utf-8
from collections import Counter
from nose.tools import (
    set_trace,
    eq_,
    assert_raises,
    assert_raises_regexp,
)
import json
import datetime

from . import (
    DatabaseTest,
)

from core.classifier import Classifier
from core.entrypoint import AudiobooksEntryPoint
from core.external_search import Filter
from core.lane import (
    Facets,
    Lane,
    WorkList,
)
from core.metadata_layer import (
    ContributorData,
    Metadata,
)
from core.lane import FacetsWithEntryPoint
from core.model import (
    create,
    Contribution,
    Contributor,
    Edition,
    SessionManager,
    DataSource,
    ExternalIntegration,
    Library,
    MaterializedWorkWithGenre,
)

from api.config import (
    Configuration,
    CannotLoadConfiguration,
    temp_config,
)
from api.lanes import (
    create_default_lanes,
    create_lanes_for_large_collection,
    create_lane_for_small_collection,
    create_lane_for_tiny_collection,
    create_world_languages_lane,
    _lane_configuration_from_collection_sizes,
    load_lanes,
    ContributorFacets,
    ContributorLane,
    CrawlableCollectionBasedLane,
    CrawlableFacets,
    CrawlableCustomListBasedLane,
    RecommendationLane,
    RelatedBooksLane,
    SeriesFacets,
    SeriesLane,
    WorkBasedLane,
)
from api.novelist import MockNoveListAPI


class TestLaneCreation(DatabaseTest):

    def test_create_lanes_for_large_collection(self):
        languages = ['eng', 'spa']
        create_lanes_for_large_collection(self._db, self._default_library, languages)
        lanes = self._db.query(Lane).filter(Lane.parent_id==None).order_by(Lane.priority).all()

        # We have five top-level lanes.
        eq_(5, len(lanes))
        eq_(
            ['Fiction', 'Nonfiction', 'Young Adult Fiction',
             'Young Adult Nonfiction', 'Children and Middle Grade'],
            [x.display_name for x in lanes]
        )
        for lane in lanes:
            eq_(self._default_library, lane.library)
            # They all are restricted to English and Spanish.
            eq_(x.languages, languages)

            # They have no restrictions on media type -- that's handled
            # with entry points.
            eq_(None, x.media)

        eq_(
            ['Fiction', 'Nonfiction', 'Young Adult Fiction',
             'Young Adult Nonfiction', 'Children and Middle Grade'],
            [x.display_name for x in lanes]
        )


        # The Adult Fiction and Adult Nonfiction lanes reproduce the
        # genre structure found in the genre definitions.
        fiction, nonfiction = lanes[0:2]
        [sf] = [x for x in fiction.sublanes if 'Science Fiction' in x.display_name]
        [periodicals] = [x for x in nonfiction.sublanes if 'Periodicals' in x.display_name]
        eq_(True, sf.fiction)
        eq_("Science Fiction", sf.display_name)
        assert 'Science Fiction' in [genre.name for genre in sf.genres]

        [nonfiction_humor] = [x for x in nonfiction.sublanes
                              if 'Humor' in x.display_name]
        eq_(False, nonfiction_humor.fiction)

        [fiction_humor] = [x for x in fiction.sublanes
                           if 'Humor' in x.display_name]
        eq_(True, fiction_humor.fiction)

        [space_opera] = [x for x in sf.sublanes if 'Space Opera' in x.display_name]
        eq_(True, sf.fiction)
        eq_("Space Opera", space_opera.display_name)
        eq_(["Space Opera"], [genre.name for genre in space_opera.genres])

        [history] = [x for x in nonfiction.sublanes if 'History' in x.display_name]
        eq_(False, history.fiction)
        eq_("History", history.display_name)
        assert 'History' in [genre.name for genre in history.genres]
        [european_history] = [x for x in history.sublanes if 'European History' in x.display_name]
        assert 'European History' in [genre.name for genre in european_history.genres]

        # Delete existing lanes.
        for lane in self._db.query(Lane).filter(Lane.library_id==self._default_library.id):
            self._db.delete(lane)

        # If there's an NYT Best Sellers integration and we create the lanes again...
        integration, ignore = create(
            self._db, ExternalIntegration, goal=ExternalIntegration.METADATA_GOAL,
            protocol=ExternalIntegration.NYT)

        create_lanes_for_large_collection(self._db, self._default_library, languages)
        lanes = self._db.query(Lane).filter(Lane.parent_id==None).order_by(Lane.priority).all()

        # Now we have six top-level lanes, with best sellers at the beginning.
        eq_(
            [u'Best Sellers', 'Fiction', 'Nonfiction', 'Young Adult Fiction',
             'Young Adult Nonfiction', 'Children and Middle Grade'],
            [x.display_name for x in lanes]
        )

        # Each sublane other than best sellers also contains a best sellers lane.
        for sublane in lanes[1:]:
            best_sellers = sublane.visible_children[0]
            eq_("Best Sellers", best_sellers.display_name)

        # The best sellers lane has a data source.
        nyt_data_source = DataSource.lookup(self._db, DataSource.NYT)
        eq_(nyt_data_source, lanes[0].list_datasource)

    def test_create_world_languages_lane(self):
        # If there are no small or tiny collections, calling
        # create_world_languages_lane does not create any lanes or change
        # the priority.
        new_priority = create_world_languages_lane(
            self._db, self._default_library, [], [], priority=10
        )
        eq_(10, new_priority)
        eq_([], self._db.query(Lane).all())

        # If there are lanes to be created, create_world_languages_lane
        # creates them.
        new_priority = create_world_languages_lane(
            self._db, self._default_library,
            ["eng"], [["spa", "fre"]], priority=10
        )

        # priority has been incremented to make room for the newly
        # created lane.
        eq_(11, new_priority)

        # One new top-level lane has been created. It contains books
        # from all three languages mentioned in its children.
        top_level = self._db.query(Lane).filter(Lane.parent==None).one()
        eq_("World Languages", top_level.display_name)
        eq_(set(['spa', 'fre', 'eng']), top_level.languages)

        # It has two children -- one for the small English collection and
        # one for the tiny Spanish/French collection.,
        small, tiny = top_level.visible_children
        eq_(u'English', small.display_name)
        eq_([u'eng'], small.languages)

        eq_(u'espa\xf1ol/fran\xe7ais', tiny.display_name)
        eq_([u'spa', u'fre'], tiny.languages)

        # The tiny collection has no sublanes, but the small one has
        # three.  These lanes are tested in more detail in
        # test_create_lane_for_small_collection.
        fiction, nonfiction, children = small.sublanes
        eq_([], tiny.sublanes)
        eq_("Fiction", fiction.display_name)
        eq_("Nonfiction", nonfiction.display_name)
        eq_("Children & Young Adult", children.display_name)

    def test_create_lane_for_small_collection(self):
        languages = ['eng', 'spa', 'chi']
        create_lane_for_small_collection(
            self._db, self._default_library, None, languages
        )
        [lane] = self._db.query(Lane).filter(Lane.parent_id==None).all()

        eq_(u"English/español/Chinese", lane.display_name)
        sublanes = lane.visible_children
        eq_(
            ['Fiction', 'Nonfiction', 'Children & Young Adult'],
            [x.display_name for x in sublanes]
        )
        for x in sublanes:
            eq_(languages, x.languages)
            eq_([Edition.BOOK_MEDIUM], x.media)

        eq_(
            [set(['Adults Only', 'Adult']),
             set(['Adults Only', 'Adult']),
             set(['Young Adult', 'Children'])],
            [set(x.audiences) for x in sublanes]
        )
        eq_([True, False, None],
            [x.fiction for x in sublanes]
        )

    def test_lane_for_tiny_collection(self):
        parent = self._lane()
        new_priority = create_lane_for_tiny_collection(
            self._db, self._default_library, parent, 'ger',
            priority=3
        )
        eq_(4, new_priority)
        lane = self._db.query(Lane).filter(Lane.parent==parent).one()
        eq_([Edition.BOOK_MEDIUM], lane.media)
        eq_(parent, lane.parent)
        eq_(['ger'], lane.languages)
        eq_(u'Deutsch', lane.display_name)
        eq_([], lane.children)

    def test_create_default_lanes(self):
        library = self._default_library
        library.setting(
            Configuration.LARGE_COLLECTION_LANGUAGES
        ).value = json.dumps(
            ['eng']
        )

        library.setting(
            Configuration.SMALL_COLLECTION_LANGUAGES
        ).value = json.dumps(
            ['spa', 'chi']
        )

        library.setting(
            Configuration.TINY_COLLECTION_LANGUAGES
        ).value = json.dumps(
            ['ger','fre','ita']
        )

        create_default_lanes(self._db, self._default_library)
        lanes = self._db.query(Lane).filter(Lane.library==library).filter(Lane.parent_id==None).all()

        # We have five top-level lanes for the large collection,
        # a top-level lane for each small collection, and a lane
        # for everything left over.
        eq_(set(['Fiction', "Nonfiction", "Young Adult Fiction", "Young Adult Nonfiction",
                 "Children and Middle Grade", u'World Languages']),
            set([x.display_name for x in lanes])
        )

        [english_fiction_lane] = [x for x in lanes if x.display_name == 'Fiction']
        eq_(0, english_fiction_lane.priority)
        [world] = [x for x in lanes if x.display_name == 'World Languages']
        eq_(5, world.priority)

    def test_lane_configuration_from_collection_sizes(self):

        # If the library has no holdings, we assume it has a large English
        # collection.
        m = _lane_configuration_from_collection_sizes
        eq_(([u'eng'], [], []), m(None))
        eq_(([u'eng'], [], []), m(Counter()))

        # Otherwise, the language with the largest collection, and all
        # languages more than 10% as large, go into `large`.  All
        # languages with collections more than 1% as large as the
        # largest collection go into `small`. All languages with
        # smaller collections go into `tiny`.
        base = 10000
        holdings = Counter(large1=base, large2=base*0.1001,
                           small1=base*0.1, small2=base*0.01001,
                           tiny=base*0.01)
        large, small, tiny = m(holdings)
        eq_(set(['large1', 'large2']), set(large))
        eq_(set(['small1', 'small2']), set(small))
        eq_(['tiny'], tiny)

class TestWorkBasedLane(DatabaseTest):

    def test_initialization_sets_appropriate_audiences(self):
        work = self._work(with_license_pool=True)

        work.audience = Classifier.AUDIENCE_CHILDREN
        children_lane = WorkBasedLane(self._default_library, work, '')
        eq_([Classifier.AUDIENCE_CHILDREN], children_lane.audiences)

        work.audience = Classifier.AUDIENCE_YOUNG_ADULT
        ya_lane = WorkBasedLane(self._default_library, work, '')
        eq_(sorted(Classifier.AUDIENCES_JUVENILE), sorted(ya_lane.audiences))

        work.audience = Classifier.AUDIENCE_ADULT
        adult_lane = WorkBasedLane(self._default_library, work, '')
        eq_(sorted(Classifier.AUDIENCES), sorted(adult_lane.audiences))

        work.audience = Classifier.AUDIENCE_ADULTS_ONLY
        adults_only_lane = WorkBasedLane(self._default_library, work, '')
        eq_(sorted(Classifier.AUDIENCES), sorted(adults_only_lane.audiences))

    def test_append_child(self):
        """When a WorkBasedLane gets a child, its language and audience
        restrictions are propagated to the child.
        """
        work = self._work(
            with_license_pool=True, audience=Classifier.AUDIENCE_CHILDREN,
            language='spa'
        )

        def make_child():
            # Set up a WorkList with settings that contradict the
            # settings of the work we'll be using as the basis for our
            # WorkBasedLane.
            child = WorkList()
            child.initialize(
                self._default_library, 'sublane', languages=['eng'],
                audiences=[Classifier.AUDIENCE_ADULT]
            )
            return child
        child1, child2 = [make_child() for i in range(2)]

        # The WorkBasedLane's restrictions are propagated to children
        # passed in to the constructor.
        lane = WorkBasedLane(self._default_library, work, 'parent lane',
                             children=[child1])

        eq_(['spa'], child1.languages)
        eq_([Classifier.AUDIENCE_CHILDREN], child1.audiences)

        # It also happens when .append_child is called after the
        # constructor.
        lane.append_child(child2)
        eq_(['spa'], child2.languages)
        eq_([Classifier.AUDIENCE_CHILDREN], child2.audiences)

    def test_default_children_list_not_reused(self):
        work = self._work()

        # By default, a WorkBasedLane has no children.
        lane1 = WorkBasedLane(self._default_library, work)
        eq_([], lane1.children)

        # Add a child...
        lane1.children.append(object)

        # Another lane for the same work gets a different, empty list
        # of children. It doesn't reuse the first lane's list.
        lane2 = WorkBasedLane(self._default_library, work)
        eq_([], lane2.children)


class TestRelatedBooksLane(DatabaseTest):

    def setup(self):
        super(TestRelatedBooksLane, self).setup()
        self.work = self._work(
            with_license_pool=True, audience=Classifier.AUDIENCE_YOUNG_ADULT
        )
        [self.lp] = self.work.license_pools
        self.edition = self.work.presentation_edition

    def test_initialization(self):
        # Asserts that a RelatedBooksLane won't be initialized for a work
        # without related books

        # A book without a series or a contributor on a circ manager without
        # NoveList recommendations raises an error.
        self._db.delete(self.edition.contributions[0])
        self._db.commit()

        assert_raises(
            ValueError, RelatedBooksLane, self._default_library, self.work, ""
        )

        # A book with a contributor initializes a RelatedBooksLane.
        luthor, i = self._contributor('Luthor, Lex')
        self.edition.add_contributor(luthor, [Contributor.EDITOR_ROLE])

        result = RelatedBooksLane(self._default_library, self.work, '')
        eq_(self.work, result.work)
        [sublane] = result.children
        eq_(True, isinstance(sublane, ContributorLane))
        eq_(sublane.contributor, luthor)

        # As does a book in a series.
        self.edition.series = "All By Myself"
        result = RelatedBooksLane(self._default_library, self.work, "")
        eq_(2, len(result.children))
        [contributor, series] = result.children
        eq_(True, isinstance(series, SeriesLane))

        # When NoveList is configured and recommendations are available,
        # a RecommendationLane will be included.
        self._external_integration(
            ExternalIntegration.NOVELIST,
            goal=ExternalIntegration.METADATA_GOAL, username=u'library',
            password=u'sure', libraries=[self._default_library]
        )
        mock_api = MockNoveListAPI(self._db)
        response = Metadata(
            self.edition.data_source, recommendations=[self._identifier()]
        )
        mock_api.setup(response)
        result = RelatedBooksLane(self._default_library, self.work, "", novelist_api=mock_api)
        eq_(3, len(result.children))

        # The book's language and audience list is passed down to all sublanes.
        eq_(['eng'], result.languages)
        for sublane in result.children:
            eq_(result.languages, sublane.languages)
            if isinstance(sublane, SeriesLane):
                eq_([result.source_audience], sublane.audiences)
            else:
                eq_(sorted(list(result.audiences)), sorted(list(sublane.audiences)))

        contributor, recommendations, series = result.children
        eq_(True, isinstance(recommendations, RecommendationLane))
        eq_(True, isinstance(series, SeriesLane))
        eq_(True, isinstance(contributor, ContributorLane))

    def test_contributor_lane_generation(self):

        original = self.edition.contributions[0].contributor
        luthor, i = self._contributor('Luthor, Lex')
        self.edition.add_contributor(luthor, Contributor.EDITOR_ROLE)

        # Lex Luthor doesn't show up because he's only an editor,
        # and an author is listed.
        result = RelatedBooksLane(self._default_library, self.work, '')
        eq_(1, len(result.children))
        [sublane] = result.children
        eq_(original, sublane.contributor)

        # A book with multiple contributors results in multiple
        # ContributorLane sublanes.
        lane, i = self._contributor('Lane, Lois')
        self.edition.add_contributor(lane, Contributor.PRIMARY_AUTHOR_ROLE)
        result = RelatedBooksLane(self._default_library, self.work, '')
        eq_(2, len(result.children))
        sublane_contributors = list()
        [sublane_contributors.append(c.contributor) for c in result.children]
        eq_(set([lane, original]), set(sublane_contributors))

        # When there are no AUTHOR_ROLES present, contributors in
        # displayable secondary roles appear.
        for contribution in self.edition.contributions:
            if contribution.role in Contributor.AUTHOR_ROLES:
                self._db.delete(contribution)
        self._db.commit()

        result = RelatedBooksLane(self._default_library, self.work, '')
        eq_(1, len(result.children))
        [sublane] = result.children
        eq_(luthor, sublane.contributor)

    def test_works_query(self):
        """RelatedBooksLane is an invisible, groups lane without works."""

        self.edition.series = "All By Myself"
        lane = RelatedBooksLane(self._default_library, self.work, "")
        eq_([], lane.works_from_database(self._db).all())


class LaneTest(DatabaseTest):

    def assert_works_queries(self, lane, expected):
        """Tests resulting Lane.works() and Lane.materialized_works() results"""

        materialized_expected = []
        if expected:
            materialized_expected = [work.id for work in expected]

        query = lane.works_from_database(self._db)
        materialized_results = [work.works_id for work in query.all()]

        eq_(sorted(materialized_expected), sorted(materialized_results))

    def sample_works_for_each_audience(self):
        """Create a work for each audience-type."""
        works = list()
        audiences = [
            Classifier.AUDIENCE_CHILDREN, Classifier.AUDIENCE_YOUNG_ADULT,
            Classifier.AUDIENCE_ADULT, Classifier.AUDIENCE_ADULTS_ONLY
        ]

        for audience in audiences:
            work = self._work(with_license_pool=True, audience=audience,
                              data_source_name=DataSource.OVERDRIVE)
            works.append(work)

        return works


class TestRecommendationLane(LaneTest):

    def setup(self):
        super(TestRecommendationLane, self).setup()
        self.work = self._work(with_license_pool=True)

    def generate_mock_api(self):
        """Prep an empty NoveList result."""
        source = DataSource.lookup(self._db, DataSource.OVERDRIVE)
        metadata = Metadata(source)

        mock_api = MockNoveListAPI(self._db)
        mock_api.setup(metadata)
        return mock_api

    def test_works_query(self):
        # Prep an empty result.
        mock_api = self.generate_mock_api()

        # With an empty recommendation result, the lane is empty.
        lane = RecommendationLane(self._default_library, self.work, '', novelist_api=mock_api)
        eq_([], lane.works_from_database(self._db).all())

        # Resulting recommendations are returned when available, though.
        # TODO: Setting a data source name is necessary because Gutenberg
        # books get filtered out when children or ya is one of the lane's
        # audiences.
        result = self._work(with_license_pool=True, data_source_name=DataSource.OVERDRIVE)
        lane.recommendations = [result.license_pools[0].identifier]
        SessionManager.refresh_materialized_views(self._db)
        self.assert_works_queries(lane, [result])

    def test_works_query_with_source_audience(self):

        # If the lane is created with a source audience, it filters the
        # recommendations appropriately.
        works = self.sample_works_for_each_audience()
        [children, ya, adult, adults_only] = works
        recommendations = list()
        for work in works:
            recommendations.append(work.license_pools[0].identifier)

        expected = {
            Classifier.AUDIENCE_CHILDREN : [children],
            Classifier.AUDIENCE_YOUNG_ADULT : [children, ya],
            Classifier.AUDIENCE_ADULTS_ONLY : works
        }

        for audience, results in expected.items():
            self.work.audience = audience
            SessionManager.refresh_materialized_views(self._db)

            mock_api = self.generate_mock_api()
            lane = RecommendationLane(
                self._default_library, self.work, '', novelist_api=mock_api
            )
            lane.recommendations = recommendations
            self.assert_works_queries(lane, results)

    def test_works_query_with_source_language(self):
        # Prepare a number of works with different languages.
        # TODO: Setting a data source name is necessary because
        # Gutenberg books get filtered out when children or ya
        # is one of the lane's audiences.
        eng = self._work(with_license_pool=True, language='eng', data_source_name=DataSource.OVERDRIVE)
        fre = self._work(with_license_pool=True, language='fre', data_source_name=DataSource.OVERDRIVE)
        spa = self._work(with_license_pool=True, language='spa', data_source_name=DataSource.OVERDRIVE)
        SessionManager.refresh_materialized_views(self._db)

        # They're all returned as recommendations from NoveList Select.
        recommendations = list()
        for work in [eng, fre, spa]:
            recommendations.append(work.license_pools[0].identifier)

        # But only the work that matches the source work is included.
        mock_api = self.generate_mock_api()
        lane = RecommendationLane(self._default_library, self.work, '', novelist_api=mock_api)
        lane.recommendations = recommendations
        self.assert_works_queries(lane, [eng])

        # It doesn't matter the language.
        self.work.presentation_edition.language = 'fre'
        SessionManager.refresh_materialized_views(self._db)
        mock_api = self.generate_mock_api()
        lane = RecommendationLane(self._default_library, self.work, '', novelist_api=mock_api)
        lane.recommendations = recommendations
        self.assert_works_queries(lane, [fre])

class TestSeriesFacets(DatabaseTest):

    def setup(self):
        # Set up a generic SeriesFacets object.
        super(TestSeriesFacets, self).setup()
        library = self._default_library
        self.worklist = SeriesLane(library, "Snake Eyes")
        args = {}
        self.facets = SeriesFacets.from_request(
            library, library, args.get, args.get, self.worklist
        )
        assert isinstance(self.facets, SeriesFacets)

    def test_class_methods(self):
        config = self._default_library
        # In general, SeriesFacets has the same options and defaults
        # as a normal Facets object.
        for group_name in (Facets.COLLECTION_FACET_GROUP_NAME,
                           Facets.AVAILABILITY_FACET_GROUP_NAME):
            eq_(Facets.available_facets(config, group_name),
                SeriesFacets.available_facets(config, group_name))
            eq_(Facets.default_facet(config, group_name),
                SeriesFacets.default_facet(config, group_name))

        # However, SeriesFacets has an extra sort option -- you can
        # sort by series position.
        group_name = Facets.ORDER_FACET_GROUP_NAME
        default = Facets.available_facets(config, group_name)
        series = SeriesFacets.available_facets(config, group_name)
        eq_([SeriesFacets.ORDER_SERIES_POSITION] + default, series)

        # This is the default sort option for SeriesFacets.
        eq_(SeriesFacets.ORDER_SERIES_POSITION,
            SeriesFacets.default_facet(config, group_name))

    def test_instantiation_and_navigation(self):
        # When a SeriesFacets is instantiated for a SeriesLane,
        # the series associated with the SeriesLane is copied to the
        # SeriesFacets.
        eq_("Snake Eyes", self.facets.series)

        # Navigating to another entry point gets us another SeriesFacets
        # for the same series.
        new_facets = self.facets.navigate(entrypoint=AudiobooksEntryPoint)
        assert isinstance(new_facets, SeriesFacets)
        eq_("Snake Eyes", new_facets.series)
        eq_(AudiobooksEntryPoint, new_facets.entrypoint)

    def test_modify_search_filter(self):
        filter = Filter()
        self.facets.modify_search_filter(filter)
        eq_("Snake Eyes", filter.series)


class TestSeriesLane(LaneTest):

    def test_initialization(self):
        # An error is raised if SeriesLane is created with an empty string.
        assert_raises(
            ValueError, SeriesLane, self._default_library, ''
        )
        assert_raises(
            ValueError, SeriesLane, self._default_library, None
        )

        work = self._work(
            language='spa', audience=[Classifier.AUDIENCE_CHILDREN]
        )
        work_based_lane = WorkBasedLane(self._default_library, work)
        child = SeriesLane(self._default_library, "Alrighty Then",
                           parent=work_based_lane, languages=['eng'],
                           audiences=['another audience'])

        # The series provided in the constructor is stored as .series.
        eq_("Alrighty Then", child.series)

        # The SeriesLane is added as a child of its parent
        # WorkBasedLane -- something that doesn't happen by default.
        eq_([child], work_based_lane.children)

        # As a side effect of that, this lane's audiences and
        # languages were changed to values consistent with its parent.
        eq_([work_based_lane.source_audience], child.audiences)
        eq_(work_based_lane.languages, child.languages)


class TestContributorFacets(DatabaseTest):

    def setup(self):
        # Set up a generic ContributorFacets object.
        super(TestContributorFacets, self).setup()
        library = self._default_library
        self.contributor_data = ContributorData(display_name="An Author")
        self.worklist = ContributorLane(library, self.contributor_data)
        args = {}
        self.facets = ContributorFacets.from_request(
            library, library, args.get, args.get, self.worklist
        )
        assert isinstance(self.facets, ContributorFacets)

    def test_instantiation_and_navigation(self):
        # When a ContributorFacets is instantiated for a ContributorLane,
        # the series associated with the ContributorLane is copied to the
        # ContributorFacets.
        eq_(self.contributor_data, self.facets.contributor)

        # Navigating to another entry point gets us another ContributorFacets
        # for the same series.
        new_facets = self.facets.navigate(entrypoint=AudiobooksEntryPoint)
        assert isinstance(new_facets, ContributorFacets)
        eq_(self.contributor_data, new_facets.contributor)
        eq_(AudiobooksEntryPoint, new_facets.entrypoint)

    def test_modify_search_filter(self):
        filter = Filter()
        self.facets.modify_search_filter(filter)
        eq_(self.contributor_data, filter.author)


class TestContributorLane(LaneTest):

    def setup(self):
        super(TestContributorLane, self).setup()
        self.contributor, i = self._contributor(
            'Lane, Lois', **dict(viaf='7', display_name='Lois Lane')
        )

    def test_initialization(self):
        assert_raises_regexp(
            ValueError, 
            "ContributorLane can't be created without contributor",
            ContributorLane,
            self._default_library,
            None
        )

        parent = WorkList()
        parent.initialize(self._default_library)

        lane = ContributorLane(
            self._default_library, self.contributor, parent,
            languages=['a'], audiences=['b'],
        )
        eq_(self.contributor, lane.contributor)
        eq_(['a'], lane.languages)
        eq_(['b'], lane.audiences)
        eq_([lane], parent.children)

        # The contributor_key will be used in links to other pages
        # of this Lane and so on.
        eq_("Lois Lane", lane.contributor_key)

        # If the contributor used to create a ContributorLane has no
        # display name, their sort name is used as the
        # contributor_key.
        contributor = ContributorData(sort_name="Lane, Lois")
        lane = ContributorLane(self._default_library, contributor)
        eq_(contributor, lane.contributor)
        eq_("Lane, Lois", lane.contributor_key)

    def test_url_arguments(self):
        lane = ContributorLane(
            self._default_library, self.contributor,
            languages=['eng', 'spa'], audiences=['Adult', 'Children'],
        )
        route, kwargs = lane.url_arguments
        eq_(lane.ROUTE, route)

        eq_(
            dict(
                contributor_name=lane.contributor_key,
                languages='eng,spa',
                audiences='Adult,Children'
            ),
            kwargs
        )


class TestCrawlableFacets(DatabaseTest):

    def test_default(self):
        facets = CrawlableFacets.default(self._default_library)
        eq_(CrawlableFacets.COLLECTION_FULL, facets.collection)
        eq_(CrawlableFacets.AVAILABLE_ALL, facets.availability)
        eq_(CrawlableFacets.ORDER_LAST_UPDATE, facets.order)
        eq_(False, facets.order_ascending)

        # There's only one enabled value for each facet group.
        for group in facets.enabled_facets:
            eq_(1, len(group))


class TestCrawlableCollectionBasedLane(DatabaseTest):

    def test_init(self):

        # Collection-based crawlable feeds are cached for 2 hours.
        eq_(2 * 60 * 60, CrawlableCollectionBasedLane.MAX_CACHE_AGE)

        # This library has two collections.
        library = self._default_library
        default_collection = self._default_collection
        other_library_collection = self._collection()
        library.collections.append(other_library_collection)

        # This collection is not associated with any library.
        unused_collection = self._collection()

        # A lane for all the collections associated with a library.
        lane = CrawlableCollectionBasedLane()
        lane.initialize(library)
        eq_("Crawlable feed: %s" % library.name, lane.display_name)
        eq_(set([x.id for x in library.collections]), set(lane.collection_ids))

        # A lane for specific collection, regardless of their library
        # affiliation.
        lane = CrawlableCollectionBasedLane()
        lane.initialize([unused_collection, other_library_collection])
        eq_(
            "Crawlable feed: %s / %s" % tuple(
                sorted([unused_collection.name, other_library_collection.name])
            ),
            lane.display_name
        )
        eq_(set([unused_collection.id, other_library_collection.id]),
            set(lane.collection_ids))

        # Unlike pretty much all other lanes in the system, this lane
        # has no affiliated library.
        eq_(None, lane.get_library(self._db))

    def test_url_arguments(self):
        library = self._default_library
        other_collection = self._collection()

        # A lane for all the collections associated with a library.
        lane = CrawlableCollectionBasedLane()
        lane.initialize(library)
        route, kwargs = lane.url_arguments
        eq_(CrawlableCollectionBasedLane.LIBRARY_ROUTE, route)
        eq_(None, kwargs.get("collection_name"))

        # A lane for a collection not actually associated with a
        # library.
        lane = CrawlableCollectionBasedLane()
        lane.initialize([other_collection])
        route, kwargs = lane.url_arguments
        eq_(CrawlableCollectionBasedLane.COLLECTION_ROUTE, route)
        eq_(other_collection.name, kwargs.get("collection_name"))


class TestCrawlableCustomListBasedLane(DatabaseTest):

    def test_initialize(self):
        # These feeds are cached for 12 hours.
        eq_(12 * 60 * 60, CrawlableCustomListBasedLane.MAX_CACHE_AGE)

        customlist, ignore = self._customlist()
        lane = CrawlableCustomListBasedLane()
        lane.initialize(self._default_library, customlist)
        eq_(self._default_library.id, lane.library_id)
        eq_([customlist.id], lane.customlist_ids)
        eq_(customlist.name, lane.customlist_name)
        eq_("Crawlable feed: %s" % customlist.name, lane.display_name)
        eq_(None, lane.audiences)
        eq_(None, lane.languages)
        eq_(None, lane.media)
        eq_([], lane.children)

    def test_url_arguments(self):
        customlist, ignore = self._customlist()
        lane = CrawlableCustomListBasedLane()
        lane.initialize(self._default_library, customlist)
        route, kwargs = lane.url_arguments
        eq_(CrawlableCustomListBasedLane.ROUTE, route)
        eq_(customlist.name, kwargs.get("list_name"))


