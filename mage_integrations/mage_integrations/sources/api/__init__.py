import json
import math
import time
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO, StringIO
from typing import Dict, Generator, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import magic
import pandas as pd
import polars
import requests
from singer.schema import Schema

from mage_integrations.sources.base import Source, main
from mage_integrations.sources.catalog import Catalog, CatalogEntry
from mage_integrations.sources.constants import (
    COLUMN_TYPE_ARRAY,
    COLUMN_TYPE_OBJECT,
    COLUMN_TYPE_STRING,
    REPLICATION_METHOD_FULL_TABLE,
    UNIQUE_CONFLICT_METHOD_UPDATE,
)
from mage_integrations.sources.utils import get_standard_metadata
from mage_integrations.transformers.utils import convert_data_type, infer_dtypes
from mage_integrations.utils.dictionary import dig

DEFAULT_TIMEOUT = 12

DEFAULT_RETRY_CONFIG = dict(
    max_attempts=5,
    base_delay=1.0,
    backoff_factor=2.0,
    max_delay=60.0,
    respect_retry_after=True,
    retry_statuses=[429, 500, 502, 503, 504],
)

PAGINATION_TYPE_PAGE = 'page'
PAGINATION_TYPE_OFFSET = 'offset'
PAGINATION_TYPE_CURSOR = 'cursor'
PAGINATION_TYPE_NEXT_URL = 'next_url'
PAGINATION_TYPES = [
    PAGINATION_TYPE_PAGE,
    PAGINATION_TYPE_OFFSET,
    PAGINATION_TYPE_CURSOR,
    PAGINATION_TYPE_NEXT_URL,
]

DEFAULT_PAGINATION_CONFIG = dict(
    type=None,
    max_pages=100,
    stop_when_empty=True,
    # type: page
    page_param='page',
    first_page=1,
    page_size_param=None,
    page_size=None,
    total_pages_path=None,
    # type: offset
    offset_param='offset',
    offset_start=0,
    limit_param='limit',
    limit=100,
    total_count_path=None,
    # type: cursor
    cursor_param='cursor',
    cursor_path=None,
    cursor_start=None,
    # type: next_url
    next_url_path=None,
)


class Api(Source):
    @property
    def http_method(self):
        return 'post' if self.config.get('method', 'GET').upper() == 'POST' else 'get'

    @property
    def timeout(self) -> int:
        return self.config.get('timeout') or DEFAULT_TIMEOUT

    @property
    def retry_config(self) -> Dict:
        config = dict(DEFAULT_RETRY_CONFIG)
        if self.config.get('retry'):
            config.update({k: v for k, v in self.config['retry'].items() if v is not None})
        return config

    @property
    def pagination_config(self) -> Optional[Dict]:
        if not self.config.get('pagination'):
            return None

        config = dict(DEFAULT_PAGINATION_CONFIG)
        config.update({k: v for k, v in self.config['pagination'].items() if v is not None})

        if not config.get('type'):
            # Auto-detect the pagination strategy from the configured keys.
            if config.get('next_url_path'):
                config['type'] = PAGINATION_TYPE_NEXT_URL
            elif config.get('cursor_path'):
                config['type'] = PAGINATION_TYPE_CURSOR
            elif any(self.config['pagination'].get(k) is not None for k in (
                'offset_param',
                'offset_start',
                'total_count_path',
            )):
                config['type'] = PAGINATION_TYPE_OFFSET
            else:
                config['type'] = PAGINATION_TYPE_PAGE

        if config['type'] not in PAGINATION_TYPES:
            raise ValueError(
                f"Invalid pagination type '{config['type']}'. "
                f'Must be one of: {", ".join(PAGINATION_TYPES)}.',
            )

        return config

    def discover(self, streams: List[str] = None) -> Catalog:
        streams = []

        for rows in self.load_data():
            if len(rows) >= 1:

                df = pd.DataFrame(rows)

                stream_id = 'api'

                properties = {}
                for col in df.columns:
                    df_filtered = df[df[col].notnull()][[col]]

                    for k, v in infer_dtypes(df_filtered).items():
                        if 'mixed' == v:
                            counter = Counter(map(type, df_filtered[col].values))
                            dtype, count = sorted(
                                [(k, v) for k, v in dict(counter).items()],
                                key=lambda t: t[1],
                                reverse=True,
                            )[0]

                            if dtype is list:
                                col_type = COLUMN_TYPE_ARRAY
                            elif dtype is dict:
                                col_type = COLUMN_TYPE_OBJECT
                            else:
                                col_type = COLUMN_TYPE_STRING
                        else:
                            col_type = convert_data_type(v)
                        properties[k] = dict(
                            type=[
                                'null',
                                col_type,
                            ],
                        )

                schema = Schema.from_dict(dict(
                    properties=properties,
                    type='object',
                ))

                metadata = get_standard_metadata(
                    key_properties=[],
                    replication_method=REPLICATION_METHOD_FULL_TABLE,
                    schema=schema.to_dict(),
                    stream_id=stream_id,
                )
                catalog_entry = CatalogEntry(
                    key_properties=[],
                    metadata=metadata,
                    replication_method=REPLICATION_METHOD_FULL_TABLE,
                    schema=schema,
                    stream=stream_id,
                    tap_stream_id=stream_id,
                    unique_conflict_method=UNIQUE_CONFLICT_METHOD_UPDATE,
                )

                streams.append(catalog_entry)

        return Catalog(streams)

    def _deal_with_google_sheets(self, response, separator, header):
        url = self.config['url']

        if url.endswith('output=csv'):
            df = polars.read_csv(StringIO(response.content.decode()), separator=separator,
                                 has_header=header).to_pandas()
            return df
        elif url.endswith('output=tsv'):
            df = polars.read_csv(StringIO(response.content.decode()), separator='\t',
                                 has_header=header).to_pandas()
            return df
        elif url.endswith('output=xlsx'):
            df = pd.read_excel(BytesIO(response.content), header=0 if header else None)
            return df
        else:
            raise Exception('Extension not allowed, please use CSV,TSV or XLSX')

    def _check_response_type(self, response):
        """This function checks the type of response for:
        CSV,TSV,Excel file or JSON

        Args:
            response (Response): Response from given request
        """
        url = self.config['url']

        if url.startswith('https://docs.google'):
            return 'google_sheets'

        data = response.content
        mime_type = magic.Magic(mime=True).from_buffer(data)
        return mime_type

    def __build_response(self, url: str = None, query: Dict = None, payload=None):
        url = url or self.config['url']
        if query is None:
            query = self.config.get('query')
        if payload is None:
            payload = self.config.get('payload')

        headers = {
            'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) \
             Chrome/86.0.4240.111 Safari/537.36',
        }
        if self.config.get('headers') is not None:  # preventing iteration errors in headers.update
            headers.update(self.config.get('headers', {}))

        tags = dict(
            headers=headers,
            method=self.http_method,
            payload=payload,
            query=query,
            url=url,
        )

        if query:
            query_string = urlsplit(url).query
            query_existing = dict(parse_qsl(query_string, keep_blank_values=True))
            query_existing.update(query)
            query_string_new = urlencode(query_existing)
            base = url.split('?')[0]
            url = f'{base}?{query_string_new}'

        self.logger.info(f'API request {self.http_method} {url} started.', tags=tags)

        s = requests.Session()
        a = requests.adapters.HTTPAdapter(max_retries=100)
        b = requests.adapters.HTTPAdapter(max_retries=100)
        s.mount('http://', a)
        s.mount('https://', b)

        return self.__request_with_retry(s, url, payload, headers)

    def __request_with_retry(self, session, url: str, payload, headers: Dict):
        """
        Execute the HTTP request, retrying transient failures (e.g. 429 and 5xx)
        with exponential backoff. When the response contains a Retry-After
        header (seconds or HTTP-date), it is honored instead of the computed
        backoff delay.
        """
        config = self.retry_config
        max_attempts = max(1, int(config['max_attempts']))
        retry_statuses = set(config['retry_statuses'] or [])

        attempt = 0
        while True:
            attempt += 1
            response = getattr(session, self.http_method)(
                url,
                data=payload,
                headers=headers,
                timeout=self.timeout,
                verify=False,
            )

            status_code = response.status_code
            if status_code not in retry_statuses or attempt >= max_attempts:
                return response

            delay = None
            if config['respect_retry_after']:
                delay = self.__retry_after_seconds(response)
            if delay is None:
                delay = min(
                    config['base_delay'] * (config['backoff_factor'] ** (attempt - 1)),
                    config['max_delay'],
                )

            self.logger.info(
                f'API request {self.http_method} {url} returned status code {status_code}, '
                f'retrying in {delay:.2f} seconds (attempt {attempt}/{max_attempts}).',
                tags=dict(
                    attempt=attempt,
                    max_attempts=max_attempts,
                    retry_delay=delay,
                    status_code=status_code,
                    url=url,
                ),
            )
            time.sleep(delay)

    @staticmethod
    def __retry_after_seconds(response) -> Optional[float]:
        """
        Parse the Retry-After response header. The value can be a number of
        seconds or an HTTP-date. Returns None when the header is missing or
        cannot be parsed.
        """
        value = response.headers.get('Retry-After')
        if value is None:
            return None

        value = value.strip()
        try:
            seconds = float(value)
            return max(0.0, seconds) if math.isfinite(seconds) else None
        except ValueError:
            pass

        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None

        if retry_at is None:
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())

    def __response_to_rows(self, result) -> List[Dict]:
        """
        Convert a decoded JSON response into a list of records, applying the
        response_parser and columns configuration. Shared by the single
        request and the paginated code paths so both behave identically.
        """
        response_parser = self.config.get('response_parser')
        columns = self.config.get('columns')

        if result is None:
            raise Exception('API response is None.')

        if response_parser:
            result = dig(result, response_parser)

            if result is None:
                raise Exception(f'API response with parser {response_parser} is None.')

        sample = None
        requires_columns = False
        if result and len(result) >= 1:
            sample = result[0]
            requires_columns = type(sample) is not dict

        rows = []

        if requires_columns:
            if not columns or len(columns) == 0:
                col_length = 0
                if type(sample) is list:
                    max_length = 0

                    for item in result:
                        if item:
                            length = len(item)
                            if length > max_length:
                                max_length = length
                                sample = item

                    col_length = len(sample)
                else:
                    col_length = 1
                columns = [f'col{i}' for i in range(col_length)]

            for item in result:
                if type(item) is not list:
                    item = [item]
                rows.append({col: item[idx] if len(item) > idx else None
                            for idx, col in enumerate(columns)})
        else:
            rows = result

        return rows

    @staticmethod
    def __dig_response(data, path: str):
        """
        Safely extract a value from a decoded JSON response using dot notation.
        Returns None when the path is not configured or cannot be resolved.
        """
        if not path:
            return None
        try:
            return dig(data, path)
        except (AttributeError, IndexError, KeyError, TypeError):
            return None

    @staticmethod
    def __pagination_token_key(token):
        try:
            hash(token)
            return token
        except TypeError:
            return json.dumps(token, sort_keys=True, default=str)

    def __merge_pagination_params(self, query: Dict, payload, params: Dict) -> Tuple[Dict, object]:
        """
        Merge pagination parameters into the request. For POST requests with a
        dictionary payload (or no payload), the parameters are added to the
        request body; otherwise they are added to the URL query string.
        """
        if not params:
            return query, payload

        if self.http_method == 'post' and (payload is None or type(payload) is dict):
            payload_merged = dict(payload or {})
            payload_merged.update(params)
            return query, payload_merged

        query_merged = dict(query or {})
        query_merged.update(params)
        return query_merged, payload

    def __load_paginated_json(self) -> List[Dict]:
        """
        Fetch every page of a JSON API response and merge the records of all
        pages into a single list. Supports page, offset, cursor and next-url
        pagination, bounded by max_pages and protected against following
        duplicate cursors/URLs.
        """
        config = self.pagination_config
        pagination_type = config['type']

        max_pages = config.get('max_pages')
        max_pages = int(max_pages) if max_pages is not None else None
        stop_when_empty = config.get('stop_when_empty', True)

        base_url = self.config['url']
        base_query = self.config.get('query')
        base_payload = self.config.get('payload')

        page = int(config['first_page'])
        offset = int(config['offset_start'])
        limit = int(config['limit']) if config.get('limit') is not None else None
        cursor = config.get('cursor_start')
        next_url = None

        all_rows = []
        seen_tokens = set()
        if pagination_type == PAGINATION_TYPE_NEXT_URL:
            seen_tokens.add(self.__pagination_token_key(base_url))
        elif pagination_type == PAGINATION_TYPE_CURSOR and cursor is not None:
            seen_tokens.add(self.__pagination_token_key(cursor))

        page_count = 0
        while True:
            params = {}
            if pagination_type == PAGINATION_TYPE_PAGE:
                params[config['page_param']] = page
                if config.get('page_size_param') and config.get('page_size') is not None:
                    params[config['page_size_param']] = config['page_size']
            elif pagination_type == PAGINATION_TYPE_OFFSET:
                params[config['offset_param']] = offset
                if config.get('limit_param') and limit is not None:
                    params[config['limit_param']] = limit
            elif pagination_type == PAGINATION_TYPE_CURSOR and cursor is not None:
                params[config['cursor_param']] = cursor

            if pagination_type == PAGINATION_TYPE_NEXT_URL and page_count >= 1:
                # The next URL returned by the API is self-contained; follow
                # it as-is without re-applying the configured query params.
                request_url = next_url
                request_query = {}
                request_payload = base_payload
            else:
                request_url = base_url
                request_query, request_payload = self.__merge_pagination_params(
                    base_query,
                    base_payload,
                    params,
                )

            response = self.__build_response(
                url=request_url,
                query=request_query,
                payload=request_payload,
            )

            if page_count == 0:
                checked_type = self._check_response_type(response)
                if checked_type != 'application/json':
                    raise Exception(
                        'Pagination is only supported for JSON API responses, '
                        f'but the response content type is {checked_type}. Remove the '
                        'pagination configuration to sync this response as-is.',
                    )

            page_count += 1

            try:
                data = response.json()
            except Exception as err:
                raise Exception(
                    f'Failed to decode JSON response on page {page_count} '
                    f'(status code {response.status_code}): {err}',
                )

            rows = self.__response_to_rows(data) or []
            all_rows.extend(rows)

            self.logger.info(
                f'Pagination fetched page {page_count} with {len(rows)} records '
                f'({len(all_rows)} records total).',
                tags=dict(
                    page=page_count,
                    records_page=len(rows),
                    records_total=len(all_rows),
                    url=request_url,
                ),
            )

            if max_pages is not None and max_pages > 0 and page_count >= max_pages:
                self.logger.info(
                    f'Pagination stopped after reaching the maximum of {max_pages} pages.',
                )
                break

            if stop_when_empty and not rows:
                break

            has_more = True
            token = None

            if pagination_type == PAGINATION_TYPE_PAGE:
                total_pages = self.__dig_response(data, config.get('total_pages_path'))
                if total_pages is not None and page >= int(total_pages):
                    has_more = False
                else:
                    page += 1
                    token = page
            elif pagination_type == PAGINATION_TYPE_OFFSET:
                offset += limit if limit is not None else len(rows)
                token = offset
                total_count = self.__dig_response(data, config.get('total_count_path'))
                if total_count is not None and offset >= int(total_count):
                    has_more = False
            elif pagination_type == PAGINATION_TYPE_CURSOR:
                cursor = self.__dig_response(data, config.get('cursor_path'))
                if cursor is None or (isinstance(cursor, str) and not cursor.strip()):
                    has_more = False
                else:
                    token = cursor
            elif pagination_type == PAGINATION_TYPE_NEXT_URL:
                url_next = self.__dig_response(data, config.get('next_url_path'))
                if not url_next or (isinstance(url_next, str) and not url_next.strip()):
                    has_more = False
                else:
                    next_url = urljoin(base_url, str(url_next))
                    token = next_url

            if not has_more:
                break

            token_key = self.__pagination_token_key(token)
            if token_key in seen_tokens:
                self.logger.info(
                    f'Pagination stopped: {token} was already requested '
                    '(duplicate cursor or URL detected).',
                )
                break
            seen_tokens.add(token_key)

        return all_rows

    def load_data(
        self,
        *args,
        **kwargs,
    ) -> Generator[List[Dict], None, None]:
        url = self.config['url']

        separator = self.config.get('separator')
        if separator is None:
            separator = ','

        header = self.config.get('has_header')
        if header is None:
            header = False

        if self.pagination_config is not None:
            rows = self.__load_paginated_json()
            self.logger.info(f'API request {self.http_method} {url} completed.')
            yield rows
            return

        response = self.__build_response()

        checked_type = self._check_response_type(response)

        if checked_type == 'text/plain' or checked_type == 'text/csv':
            df = polars.read_csv(StringIO(response.content.decode()), separator=separator,
                                 has_header=header).to_pandas()
            yield df.to_dict(orient='records')

        elif checked_type == 'google_sheets':
            df = self._deal_with_google_sheets(response, separator, header)
            yield df.to_dict(orient='records')

        elif checked_type == 'application/gzip':
            df = polars.read_csv(BytesIO(response.content),
                                 sepr=separator,
                                 has_header=header,).to_pandas()
            yield df.to_dict(orient='records')

        elif checked_type == 'application/json':
            result = response.json()
            rows = self.__response_to_rows(result)

            self.logger.info(f'API request {self.http_method} {url} completed.')

            yield rows

        else:
            try:
                df = pd.read_excel(BytesIO(response.content), header=0 if header else None)
                yield df.to_dict(orient='records')
            except Exception:
                raise Exception(f'Problems reading file {checked_type}. Check if extension is XLSX')

    def test_connection(self):
        response = self.__build_response()
        if response.status_code != 200:
            raise Exception(f'API response status code is {response.status_code}, must be 200.')


if __name__ == '__main__':
    main(Api)
