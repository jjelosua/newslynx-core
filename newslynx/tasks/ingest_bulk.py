import gevent
import gevent.monkey
gevent.monkey.patch_all()
from gevent.pool import Pool

import time

from functools import partial
from rq.timeouts import JobTimeoutException
from sqlalchemy.orm.exc import ObjectDeletedError
from psycopg2 import IntegrityError

from newslynx.core import queues
from newslynx.core import rds, gen_session
from newslynx.exc import (
    RequestError, InternalServerError)
from newslynx.tasks import ingest_content_item
from newslynx.tasks import ingest_event
from newslynx.tasks import ingest_metric
from newslynx.util import gen_uuid
from newslynx.logs import log
from newslynx.lib.serialize import (
    jsongz_to_obj, obj_to_jsongz)


class BulkLoader(object):
    __module__ = 'newslynx.tasks.bulk'
    returns = None  # either "model" or "query"
    timeout = 1000  # seconds
    result_ttl = 60  # seconds
    kwargs_ttl = 1000  # in case there is a backup in the queue
    max_workers = 5
    concurrent = True
    kwargs_key = 'rq:kwargs:{}'
    q = queues.get('bulk')
    redis = rds

    def load_one(self, item, **kw):
        """
        The method to overwrite.
        """
        raise NotImplemented

    def _load_one(self, item, **kw):
        """
        A wrapper which will catch errors
        and bubble them up
        """
        try:
            return False, self.load_one(item, **kw)
        except Exception as e:
            return True, e
        except ObjectDeletedError:
            log.warning('An object was deleted in the process of executing bulk upload.')
            return False, None

    def load_all(self, kwargs_key):
        """
        Do the work.
        """
        start = time.time()
        try:
            # create a session specific to this task
            session = gen_session()

            # get the inputs from redis
            kwargs = self.redis.get(kwargs_key)
            if not kwargs:
                raise InternalServerError(
                    'An unexpected error occurred while processing bulk upload.'
                )

            kwargs = jsongz_to_obj(kwargs)
            data = kwargs.get('data')
            kw = kwargs.get('kw')

            # delete them
            self.redis.delete(kwargs_key)

            outputs = []
            errors = []

            fx = partial(self._load_one, **kw)

            if self.concurrent:
                pool = Pool(min([len(data), self.max_workers]))
                for err, res in pool.imap_unordered(fx, data):
                    if err:
                        errors.append(res)
                    else:
                        outputs.append(res)
            else:
                for item in data:
                    err, res = fx(item)
                    if err:
                        errors.append(res)
                    else:
                        outputs.append(res)

            # return errors
            if len(errors):
                return RequestError(
                    'There was an error while bulk uploading: '
                    '{}'.format(errors[0].message))

            # add objects and execute
            if self.returns == 'model':
                for o in outputs:
                    if o is not None:
                        try:
                            session.add(o)
                        except Exception as e:
                            return RequestError(
                                'There was an error while bulk uploading: {}'
                                .format(e.message))

            # union all queries
            elif self.returns == 'query':
                for query in outputs:
                    if query is not None:
                        try:
                            session.execute(query)
                        except Exception as e:
                            return RequestError(
                                'There was an error while bulk uploading: {}'
                                .format(e.message))
            try:
                session.commit()

            except Exception as e:
                session.rollback()
                session.remove()
                return RequestError(
                    'There was an error while bulk uploading: {}'
                    .format(e.message))

            # return true if everything worked.
            session.remove()
            return True

        except JobTimeoutException:
            end = time.time()
            return InternalServerError(
                'Bulk loading timed out after {} seconds'
                .format(end-start))

    def run(self, data, **kw):

        # store the data + kwargs in redis temporarily
        # this makes the enqueuing process much, much more
        # efficient by allowing us to only pass a single key
        # into the queue rather than a massive dump of data
        # however it also means that all kwargs must be
        # json serializable
        job_id = gen_uuid()
        kwargs_key = self.kwargs_key.format(job_id)
        kwargs = {'data': data, 'kw': kw}
        rds.set(kwargs_key, obj_to_jsongz(kwargs), ex=self.kwargs_ttl)

        # send the job to the task queue
        self.q.enqueue(
            self.load_all, kwargs_key,
            job_id=job_id, timeout=self.timeout,
            result_ttl=self.result_ttl)

        return job_id


class ContentTimeseriesBulkLoader(BulkLoader):

    returns = 'query'
    timeout = 240

    def load_one(self, item, **kw):

        return ingest_metric.content_timeseries(item, **kw)


class ContentSummaryBulkLoader(BulkLoader):

    returns = 'query'
    timeout = 480

    def load_one(self, item, **kw):

        return ingest_metric.content_summary(item, **kw)


class OrgTimeseriesBulkLoader(BulkLoader):

    returns = 'query'
    timeout = 240

    def load_one(self, item, **kw):

        return ingest_metric.org_timeseries(item, **kw)


class OrgSummaryBulkLoader(BulkLoader):

    returns = 'query'
    timeout = 120

    def load_one(self, item, **kw):

        return ingest_metric.org_summary(item, **kw)


class EventBulkLoader(BulkLoader):

    returns = 'model'
    timeout = 480

    def load_one(self, item, **kw):
        return ingest_event.ingest(item, **kw)


class ContentItemBulkLoader(BulkLoader):

    returns = 'model'
    timeout = 240
    concurrent = True

    def load_one(self, item, **kw):
        return ingest_content_item.ingest(item, **kw)


# make sure the functions are importable + pickleable
content_timeseries = ContentTimeseriesBulkLoader().run
content_summary = ContentSummaryBulkLoader().run
org_timeseries = OrgTimeseriesBulkLoader().run
org_summary = OrgSummaryBulkLoader().run
events = EventBulkLoader().run
content_items = ContentItemBulkLoader().run
