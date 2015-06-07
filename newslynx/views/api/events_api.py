from gevent.pool import Pool

import copy
import logging

from flask import Blueprint

from newslynx.core import db
from newslynx.exc import RequestError, NotFoundError
from newslynx.models import Event, Tag, SousChef, Recipe, ContentItem
from newslynx.models.relations import events_tags, content_items_events
from newslynx.models.util import get_table_columns
from newslynx.lib.serialize import jsonify
from newslynx.views.decorators import load_user, load_org
from newslynx.views.util import *
from newslynx.tasks import facet
from newslynx.tasks.ingest_event import ingest_event
from newslynx.constants import EVENT_FACETS

# blueprint
bp = Blueprint('events', __name__)

log = logging.getLogger(__name__)

# utils
event_facet_pool = Pool(len(EVENT_FACETS))


def apply_event_filters(q, **kw):
    """
    Given a base Event.query, apply all filters.
    """

    # filter by org_id
    q = q.filter(Event.org_id == kw['org_id'])

    # apply search query
    if kw['search_query']:
        if kw['sort_field'] == 'relevance':
            sort = True
        else:
            sort = False
        if kw['search_vector'] == 'all':
            vector = Event.title_search_vector | \
                Event.description_search_vector | \
                Event.body_search_vector | \
                Event.authors_search_vector | \
                Event.meta_search_vector
        else:
            vname = "{}_search_vector".format(kw['search_vector'])
            vector = getattr(Event, vname)

        q = q.search(kw['search_query'], vector=vector, sort=sort)

    # apply status filter
    if kw['status'] != 'all':
        q = q.filter(Event.status == kw['status'])

    if kw['provenance']:
        q = q.filter(Event.provenance == kw['provenance'])

    # apply date filters
    if kw['created_after']:
        q = q.filter(Event.created >= kw['created_after'])

    if kw['created_before']:
        q = q.filter(Event.created <= kw['created_before'])

    if kw['updated_after']:
        q = q.filter(Event.updated >= kw['updated_after'])

    if kw['updated_before']:
        q = q.filter(Event.updated <= kw['updated_before'])

    # apply recipe filter
    if len(kw['include_recipes']):
        q = q.filter(Event.recipe_id.in_(kw['include_recipes']))

    if len(kw['exclude_recipes']):
        q = q.filter(~Event.recipe_id.in_(kw['exclude_recipes']))

    # apply tag categories/levels filter
    # TODO don't use multiple queries here.
    if len(kw['include_categories']):
        tag_ids = db.session.query(Tag.id).filter_by(org_id=kw['org_id'])\
            .filter(Tag.category.in_(kw['include_categories']))\
            .all()
        tag_ids = [t[0] for t in tag_ids]
        q = q.filter(Event.tags.any(Tag.id.in_(tag_ids)))

    if len(kw['exclude_categories']):
        tag_ids = db.session.query(Tag.id).filter_by(org_id=kw['org_id'])\
            .filter(Tag.category.in_(kw['exclude_categories']))\
            .all()
        tag_ids = [t[0] for t in tag_ids]
        q = q.filter(~Event.tags.any(Tag.id.in_(tag_ids)))

    if len(kw['include_levels']):
        tag_ids = db.session.query(Tag.id).filter_by(org_id=kw['org_id'])\
            .filter(Tag.level.in_(kw['include_levels']))\
            .all()
        tag_ids = [t[0] for t in tag_ids]
        q = q.filter(Event.tags.any(Tag.id.in_(tag_ids)))

    if len(kw['exclude_levels']):
        tag_ids = db.session.query(Tag.id).filter_by(org_id=kw['org_id'])\
            .filter(Tag.level.in_(kw['exclude_levels']))\
            .all()
        tag_ids = [t[0] for t in tag_ids]
        q = q.filter(~Event.tags.any(Tag.id.in_(tag_ids)))

    # apply tags filter
    if len(kw['include_tags']):
        q = q.filter(Event.tags.any(Tag.id.in_(kw['include_tags'])))

    if len(kw['exclude_tags']):
        q = q.filter(~Event.tags.any(Tag.id.in_(kw['exclude_tags'])))

    # apply things filter
    if len(kw['include_content_items']):
        q = q.filter(Event.content_items.any(
            ContentItem.id.in_(kw['include_content_items'])))

    if len(kw['exclude_content_items']):
        q = q.filter(~Event.content_items.any(
            ContentItem.id.in_(kw['exclude_content_items'])))

    # apply sous_chefs filter
    # TODO: DONT USE MULTIPLE QUERIES HERE
    if len(kw['include_sous_chefs']):
        sous_chef_recipes = db.session.query(Recipe.id)\
            .filter(Recipe.sous_chef.has(SousChef.id.in_(kw['include_sous_chefs'])))
        q = q.filter(
            Event.recipe_id.in_([r[0] for r in sous_chef_recipes.all()]))

    if len(kw['exclude_sous_chefs']):
        sous_chef_recipes = db.session.query(Recipe.id)\
            .filter(Recipe.sous_chef.has(SousChef.id.in_(kw['exclude_sous_chefs'])))\
            .all()
        q = q.filter(~Event.recipe_id.in_([r[0] for r in sous_chef_recipes]))

    return q


# endpoints


@bp.route('/api/v1/events', methods=['GET'])
@load_user
@load_org
def search_events(user, org):
    """
    args:
        q                | search query
        search           | a search vector to search on, choose from authors, title, description, body, meta, or all, default=all
        fields           | a comma-separated list of fields to include in response
        page             | page number
        per_page         | number of items per page.
        sort             | variable to order by, preface with '-' to sort desc.
        created_after    | isodate to filter results after
        created_before   | isodate to filter results before
        updated_after    | isodate to filter results after
        updated_before   | isodate to filter results before
        status           | ['pending', 'approved', 'deleted']
        provenance       | ['recipe', 'manual']
        facets           | a comma-separated list of facets to include, default=all
        tag              | a comma-separated list of tags to filter by
        categories       | a comma-separated list of tag_categories to filter by
        levels           | a comma-separated list of tag_levels to filter by
        tag_ids          | a comma-separated list of tag_ids to filter by
        content_item_ids | a comma-separated list of content_item_ids to filter by
        recipe_ids       | a comma-separated list of recipes to filter by
        sous_chef_ids    | a comma-separated list of sous_chefs to filter by
    """

    # parse arguments

    # store raw kwargs for generating pagination urls
    raw_kw = dict(request.args.items())
    raw_kw['apikey'] = user.apikey
    raw_kw['org'] = org.id

    # special arg tuples
    sort_field, direction = \
        arg_sort('sort', default='-created')

    include_tags, exclude_tags = \
        arg_list('tag_ids', default=[], typ=int, exclusions=True)

    include_content_items, exclude_content_items = \
        arg_list('content_item_ids', default=[], typ=int, exclusions=True)

    include_recipes, exclude_recipes = \
        arg_list('recipe_ids', default=[], typ=int, exclusions=True)

    include_sous_chefs, exclude_sous_chefs = \
        arg_list('sous_chef_ids', default=[], typ=int, exclusions=True)

    include_levels, exclude_levels = \
        arg_list('levels', default=[], typ=str, exclusions=True)

    include_categories, exclude_categories = \
        arg_list('categories', default=[], typ=str, exclusions=True)

    kw = dict(
        search_query=arg_str('q', default=None),
        search_vector=arg_str('search', default='all'),
        fields=arg_list('fields', default=None),
        page=arg_int('page', default=1),
        per_page=arg_limit('per_page'),
        sort_field=sort_field,
        direction=direction,
        created_after=arg_date('created_after', default=None),
        created_before=arg_date('created_before', default=None),
        updated_after=arg_date('updated_after', default=None),
        updated_before=arg_date('updated_before', default=None),
        status=arg_str('status', default='all'),
        provenance=arg_str('provenance', default=None),
        facets=arg_list('facets', default=[], typ=str),
        incl_body=arg_bool('incl_body', False),
        include_categories=include_categories,
        exclude_categories=exclude_categories,
        include_levels=include_levels,
        exclude_levels=exclude_levels,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        include_content_items=include_content_items,
        exclude_content_items=exclude_content_items,
        include_recipes=include_recipes,
        exclude_recipes=exclude_recipes,
        include_sous_chefs=include_sous_chefs,
        exclude_sous_chefs=exclude_sous_chefs,
        apikey=user.apikey,
        org_id=org.id
    )

    # validate arguments

    # validate sort fields are part of Event object.
    if kw['sort_field'] and kw['sort_field'] != 'relevance':
        validate_fields(Event, fields=[kw['sort_field']], suffix='to sort by')

    # validate select fields.
    if kw['fields']:
        validate_fields(Event, fields=kw['fields'], suffix='to select by')

    validate_tag_categories(kw['include_categories'])
    validate_tag_categories(kw['exclude_categories'])
    validate_tag_levels(kw['include_levels'])
    validate_tag_levels(kw['exclude_levels'])
    validate_event_status(kw['status'])
    validate_event_provenances(kw['provenance'])
    validate_event_search_vector(kw['search_vector'])

    # base query
    event_query = Event.query

    # apply filters
    event_query = apply_event_filters(event_query, **kw)

    # select event fields
    if kw['fields']:
        columns = [eval('Event.{}'.format(f)) for f in kw['fields']]
        event_query = event_query.with_entities(*columns)

    # apply sort if we havent already sorted by query relevance.
    if kw['sort_field'] != 'relevance':
        sort_obj = eval('Event.{sort_field}.{direction}'.format(**kw))
        event_query = event_query.order_by(sort_obj())

    # facets
    validate_event_facets(kw['facets'])

    if len(kw['facets']):

        if 'all' in kw['facets']:
            kw['facets'] = copy.copy(EVENT_FACETS)

        # get all event ids for computing counts
        event_ids = event_query\
            .with_entities(Event.id)\
            .all()
        event_ids = [e[0] for e in event_ids]

        # pooled facet function
        def fx(by):
            return by, facet.events(by, event_ids)

        # dict of results
        facets = {}
        for by, result in event_facet_pool.imap_unordered(fx, kw['facets']):
            facets[by] = result

    # paginate event_query
    events = event_query\
        .paginate(kw['page'], kw['per_page'], False)

    # total results
    total = events.total

    # generate pagination urls
    pagination = \
        urls_for_pagination('events.search_events', total, **raw_kw)

    # reformat entites as dictionary
    if kw['fields']:
        events = [dict(zip(kw['fields'], r)) for r in events.items]
    else:
        events = [e.to_dict(incl_body=kw['incl_body'])
                  for e in events.items]

    resp = {
        'events': events,
        'pagination': pagination,
        'total': total
    }

    if len(kw['facets']):
        resp['facets'] = facets

    return jsonify(resp)


@bp.route('/api/v1/events', methods=['POST'])
@load_user
@load_org
def create_event(user, org):
    """
    Create an event.
    """
    req_data = request_data()
    e = ingest_event(
        req_data,
        org_id=org.id,
        must_link=arg_bool('must_link', False))
    return jsonify(e)


@bp.route('/api/v1/events/<int:event_id>', methods=['GET'])
@load_user
@load_org
def get_event(user, org, event_id):
    """
    Fetch an individual event.
    """
    e = Event.query\
        .filter_by(id=event_id, org_id=org.id)\
        .first()
    if not e:
        raise NotFoundError(
            'An Event with ID {} does not exist.'
            .format(event_id))
    return jsonify(e.to_dict(incl_body=True))


@bp.route('/api/v1/events/<int:event_id>', methods=['PUT', 'PATCH'])
@load_user
@load_org
def event_update(user, org, event_id):
    """
    Modify an individual event.
    """
    e = Event.query\
        .filter_by(id=event_id, org_id=org.id)\
        .first()
    if not e:
        raise NotFoundError(
            'An Event with ID {} does not exist.'
            .format(event_id))

    # get request data
    req_data = request_data()

    # fetch tag and thing
    tag_ids = listify_data_arg('tag_ids')
    content_item_ids = listify_data_arg('content_item_ids')

    # check for current status / PUT status
    current_status = e.status
    req_status = req_data.get('status')

    if len(tag_ids) and len(content_item_ids):

        approve = True
        tags = Tag.query\
            .filter_by(org_id=org.id)\
            .filter(Tag.id.in_(tag_ids))\
            .all()
        if not len(tags):
            raise RequestError(
                'Tag(s) with ID(s) {} do(es) not exist.'
                .format(tag_ids))

        content_items = ContentItem.query\
            .filter_by(org_id=org.id)\
            .filter(ContentItem.id.in_(content_item_ids))\
            .all()
        if not len(content_items):
            raise RequestError(
                'ContentItem(s) with ID(s) {} do(es) not exist.'
                .format(tag_ids))

    # check for incomplete approval requests.
    elif current_status != 'approved' and req_status == 'approved':

        raise RequestError(
            'To approve an event you must assign it to one or more '
            'ContentItems or Tags by using the "content_item_ids" and '
            '"tag_ids" fields.')

    else:
        approve = False

    # filter out any non-columns
    columns = get_table_columns(Event)
    for k in req_data.keys():
        if k not in columns:
            req_data.pop(k)

    # if we're approving this event, override status
    if approve:
        req_data['status'] = 'approved'

    # update fields
    for k, v in req_data.items():
        setattr(e, k, v)

    # ensure no one sneakily/accidentally
    # updates an organization id
    e.org_id = org.id

    # if we're approving, add tags + things
    if approve:

        # only add things + tags that haven't already been assigned.
        for content_item, tag in zip(content_items, tags):
            if content_item.id not in e.content_item_ids:
                e.content_items.append(content_item)

            # validate tag
            if tag.type != 'impact':
                raise RequestError('Events can only be assigned Impact Tags.')

            # add it
            if tag.id not in e.tag_ids:
                e.tags.append(tag)

    # commit changes
    db.session.add(e)
    db.session.commit()

    # return modified event
    return jsonify(e)


@bp.route('/api/v1/events/<int:event_id>', methods=['DELETE'])
@load_user
@load_org
def event_delete(user, org, event_id):
    """
    Delete an individual event. Here, we do not explicitly "delete"
    the event, but update it's status instead. This will help
    when polling recipes for new events since we'll be able to ensure
    that we do not create duplicate events.
    """
    e = Event.query\
        .filter_by(id=event_id, org_id=org.id)\
        .first()
    if not e:
        raise NotFoundError(
            'An Event with ID {} does not exist.'
            .format(event_id))

    if arg_bool('force', False):
        db.session.delete(e)
        db.session.commit()
        return delete_response()

    # remove associations
    # from:
    # http://stackoverflow.com/questions/9882358/how-to-delete-rows-from-a-table-using-an-sqlalchemy-query-without-orm
    d = events_tags\
        .delete(events_tags.c.event_id == event_id)
    db.session.execute(d)

    d = content_items_events\
        .delete(content_items_events.c.event_id == event_id)
    db.session.execute(d)

    # update event
    e.status = 'deleted'
    db.session.add(e)
    db.session.commit()

    # return modified event
    return delete_response()


@bp.route('/api/v1/events/<int:event_id>/tags/<int:tag_id>', methods=['PUT', 'PATCH'])
@load_user
@load_org
def event_add_tag(user, org, event_id, tag_id):
    """
    Add a tag to an event.
    """
    e = Event.query\
        .filter_by(id=event_id, org_id=org.id)\
        .first()
    if not e:
        raise NotFoundError(
            'An Event with ID {} does not exist.'
            .format(event_id))

    if not e.status == 'approved':
        raise RequestError(
            'You must first approve an Event before adding additional Tags.')

    tag = Tag.query\
        .filter_by(id=tag_id, org_id=org.id)\
        .first()
    if not tag:
        raise RequestError(
            'Tag with ID {} does not exist.'
            .format(tag_id))

    if tag.type != 'impact':
        raise RequestError(
            'Events can only be assigned Impact Tags.')

    if tag.id not in e.tag_ids:
        e.tags.append(tag)

    db.session.add(e)
    db.session.commit()

    # return modified event
    return jsonify(e)


@bp.route('/api/v1/events/<int:event_id>/tags/<int:tag_id>',
          methods=['DELETE'])
@load_user
@load_org
def event_delete_tag(user, org, event_id, tag_id):
    """
    Remove a tag from an event.
    """
    e = Event.query\
        .filter_by(id=event_id, org_id=org.id)\
        .first()
    if not e:
        raise NotFoundError(
            'An Event with ID {} does not exist.'
            .format(event_id))

    if tag_id not in e.tag_ids:
        raise RequestError(
            'An Event with ID {} does not currently have an association '
            'with a Tag with ID {}.'
            .format(event_id, tag_id))

    for tag in e.tags:
        if tag.id == tag_id:
            e.tags.remove(tag)

    db.session.add(e)
    db.session.commit()

    # return modified event
    return jsonify(e)


@bp.route('/api/v1/events/<int:event_id>/content/<int:content_item_id>',
          methods=['PUT'])
@load_user
@load_org
def event_add_thing(user, org, event_id, content_item_id):
    """
    Add a thing to an event.
    """
    e = Event.query.filter_by(id=event_id, org_id=org.id).first()
    if not e:
        raise NotFoundError(
            'An Event with ID {} does not exist.'.format(event_id))

    if not e.status == 'approved':
        raise RequestError(
            'You must first approve an Event before adding additional ContentItems.')

    content_item = ContentItem.query\
        .filter_by(id=content_item_id, org_id=org.id)\
        .first()
    if not content_item:
        raise RequestError(
            'ContentItem with ID {} does not exist.'
            .format(content_item_id))

    if content_item.id not in e.content_item_ids:
        e.content_items.append(content_item)

    db.session.add(e)
    db.session.commit()

    # return modified event
    return jsonify(e)


@bp.route('/api/v1/events/<int:event_id>/content/<int:content_item_id>',
          methods=['DELETE'])
@load_user
@load_org
def event_delete_content_item(user, org, event_id, content_item_id):
    """
    Remove a thing from an event.
    """
    e = Event.query.filter_by(id=event_id, org_id=org.id).first()
    if not e:
        raise NotFoundError(
            'An Event with ID {} does not exist.'.format(event_id))

    if content_item_id not in e.content_item_ids:
        raise RequestError(
            'An Event with ID {} does not currently have an association '
            'with a ContentItem with ID {}'.format(event_id, content_item_id))

    for content_item in e.content_items:
        if content_item.id == content_item_id:
            e.content_items.remove(content_item)

    db.session.add(e)
    db.session.commit()

    # return modified event
    return jsonify(e)
