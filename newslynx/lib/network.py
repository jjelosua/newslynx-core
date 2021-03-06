"""
All things related to network requests
"""

from functools import wraps
import logging
import time

import requests

from newslynx import settings
from newslynx.lib.serialize import json_to_obj


log = logging.getLogger(__name__)

FAIL_ENCODING = 'ISO-8859-1'


def retry(*dargs, **dkwargs):
    """A decorator for performing http requests and catching all concievable errors.
       Useful for including in scrapers for unreliable webservers.
       @retry(attempts=3)
       def buggy_request():
           return requests.get('http://www.gooooooooooooogle.com')
       buggy_request()
       >>> None
    """
    # set defaults
    attempts = dkwargs.get('attempts', settings.BROWSER_MAX_RETRIES)
    wait = dkwargs.get('wait', settings.BROWSER_WAIT)
    backoff = dkwargs.get('backoff', settings.BROWSER_BACKOFF)
    verbose = dkwargs.get('verbose', True)
    raise_uncaught_errors = dkwargs.get('raise_uncaught_errors', False)
    null_value = dkwargs.get('null_value', None)

    # wrapper
    def wrapper(f):

        @wraps(f)
        def wrapped_func(*args, **kw):

            # defaults
            r = null_value
            tries = 0
            err = True

            # for ref problems
            bckof = backoff * 1
            wait_time = wait * 1

            while 1:

                # if we've exceeded the maximum number of tries,
                # return
                if tries == attempts:
                    if verbose:
                        log.error('Request to {} Failed after {} tries.'.format(args, tries))
                    return r

                # increment tries
                tries += 1

                # calc wait time for this step
                wait_time *= bckof

                # try the function
                try:
                    r = f(*args, **kw)
                    err = False

                except Exception as e:
                    if verbose:
                        log.warning('Exception - {} on try {}'.format(e, tries))
                    if raise_uncaught_errors:
                        raise e
                    else:
                        time.sleep(wait_time)

                # check the status code if its a response object
                if isinstance(r, requests.Response):
                    try:
                        r.raise_for_status()
                    except requests.exceptions.HTTPError as e:
                        if verbose:
                            log.warning('Bad Status Code - {}'.format(r.status_code))
                        time.sleep(wait_time)

                elif not err:
                    break

            return r

        return wrapped_func

    return wrapper


def get_request_kwargs(timeout=None, useragent=None):
    """This Wrapper method exists b/c some values in req_kwargs dict
    are methods which need to be called every time we make a request
    """
    return {
        'headers': {'User-Agent': useragent or settings.BROWSER_USER_AGENT},
        'timeout': timeout or settings.BROWSER_TIMEOUT,
        'allow_redirects': True
    }


@retry(attempts=settings.BROWSER_MAX_RETRIES)
def get(_u, **params):
    """Retrieves the html for either a url or a response object. All html
    extractions MUST come from this method due to some intricies in the
    requests module. To get the encoding, requests only uses the HTTP header
    encoding declaration requests.utils.get_encoding_from_headers() and reverts
    to ISO-8859-1 if it doesn't find one. This results in incorrect character
    encoding in a lot of cases.
    """
    session = requests.Session()

    FAIL_ENCODING = 'ISO-8859-1'

    html = None
    response = session.get(
        url=_u, params=params, **get_request_kwargs())
    if response.encoding != FAIL_ENCODING:
        html = response.text
    else:
        html = response.content
    if html is None:
        html = ''
    return html


@retry(attempts=settings.BROWSER_MAX_RETRIES)
def get_location(url):
    """
    most efficient method for unshortening a url.
    """
    r = requests.head(url)
    if r.status_code / 100 == 3 and 'Location' in r.headers:
        return r.headers['Location']
    return url


@retry(attempts=settings.BROWSER_MAX_RETRIES)
def get_json(_u, **params):
    """
    Fetches json from a url.
    """
    session = requests.Session()
    response = session.get(url=_u, params=params, **get_request_kwargs())
    obj = None
    if response.encoding != FAIL_ENCODING:
        content = response.text
    else:
        content = response.content
    try:
        obj = json_to_obj(content)
    except Exception as e:
        log.warning('Unable to parse json from {}. Messsage: {}'.format(_u, e.message))
        return obj
    return obj
