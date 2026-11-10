import unittest
from email.utils import formatdate
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs

import requests_mock

from mage_integrations.sources.api import Api


def json_page(records, **extra):
    return dict(json=records if not extra else dict(data=records, **extra), status_code=200)


class ApiPaginationTest(unittest.TestCase):
    def test_page_pagination_merges_all_pages(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            pagination=dict(
                type='page',
                page_param='page',
                first_page=1,
            ),
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items', [
                json_page([dict(id=1, name='a'), dict(id=2, name='b')]),
                json_page([dict(id=3, name='c')]),
                json_page([]),
            ])
            batches = list(source.load_data())

        self.assertEqual(
            batches,
            [[dict(id=1, name='a'), dict(id=2, name='b'), dict(id=3, name='c')]],
        )
        self.assertEqual(
            [r.qs.get('page') for r in m.request_history],
            [['1'], ['2'], ['3']],
        )

    def test_page_pagination_with_page_size_and_total_pages(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            response_parser='data',
            pagination=dict(
                type='page',
                page_param='page',
                page_size_param='per_page',
                page_size=2,
                total_pages_path='meta.total_pages',
            ),
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items', [
                json_page([dict(id=1), dict(id=2)], meta=dict(total_pages=2)),
                json_page([dict(id=3)], meta=dict(total_pages=2)),
                json_page([dict(id=4)], meta=dict(total_pages=2)),
            ])
            batches = list(source.load_data())

        # Stops after page 2 because total_pages is 2; the 3rd response is never requested.
        self.assertEqual(batches, [[dict(id=1), dict(id=2), dict(id=3)]])
        self.assertEqual(len(m.request_history), 2)
        self.assertEqual(m.request_history[0].qs.get('per_page'), ['2'])

    def test_page_pagination_respects_max_pages(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            pagination=dict(
                type='page',
                max_pages=2,
                stop_when_empty=False,
            ),
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items', [
                json_page([dict(id=1)]),
                json_page([dict(id=2)]),
                json_page([dict(id=3)]),
            ])
            batches = list(source.load_data())

        self.assertEqual(batches, [[dict(id=1), dict(id=2)]])
        self.assertEqual(len(m.request_history), 2)

    def test_offset_pagination_with_total_count(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            response_parser='data',
            pagination=dict(
                type='offset',
                offset_param='offset',
                limit_param='limit',
                limit=2,
                total_count_path='total',
            ),
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items', [
                json_page([dict(id=1), dict(id=2)], total=3),
                json_page([dict(id=3)], total=3),
                json_page([], total=3),
            ])
            batches = list(source.load_data())

        self.assertEqual(batches, [[dict(id=1), dict(id=2), dict(id=3)]])
        self.assertEqual(
            [(r.qs.get('offset'), r.qs.get('limit')) for r in m.request_history],
            [(['0'], ['2']), (['2'], ['2'])],
        )

    def test_offset_pagination_auto_detected(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            pagination=dict(
                offset_param='skip',
                limit_param='take',
                limit=5,
            ),
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items', [
                json_page([dict(id=1)]),
                json_page([]),
            ])
            list(source.load_data())

        self.assertEqual(m.request_history[0].qs.get('skip'), ['0'])
        self.assertEqual(m.request_history[0].qs.get('take'), ['5'])
        self.assertEqual(m.request_history[1].qs.get('skip'), ['5'])

    def test_cursor_pagination(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            response_parser='data',
            pagination=dict(
                type='cursor',
                cursor_param='cursor',
                cursor_path='meta.next_cursor',
            ),
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items', [
                json_page([dict(id=1)], meta=dict(next_cursor='abc')),
                json_page([dict(id=2)], meta=dict(next_cursor='def')),
                json_page([dict(id=3)], meta=dict(next_cursor=None)),
            ])
            batches = list(source.load_data())

        self.assertEqual(batches, [[dict(id=1), dict(id=2), dict(id=3)]])
        self.assertNotIn('cursor', m.request_history[0].qs)
        self.assertEqual(m.request_history[1].qs.get('cursor'), ['abc'])
        self.assertEqual(m.request_history[2].qs.get('cursor'), ['def'])

    def test_cursor_pagination_stops_on_duplicate_cursor(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            response_parser='data',
            pagination=dict(
                type='cursor',
                cursor_path='meta.next_cursor',
            ),
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items', [
                json_page([dict(id=1)], meta=dict(next_cursor='abc')),
                # The API keeps returning the same cursor: must stop instead of looping.
                json_page([dict(id=2)], meta=dict(next_cursor='abc')),
                json_page([dict(id=3)], meta=dict(next_cursor='abc')),
            ])
            batches = list(source.load_data())

        self.assertEqual(batches, [[dict(id=1), dict(id=2)]])
        self.assertEqual(len(m.request_history), 2)

    def test_next_url_pagination(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            response_parser='data',
            pagination=dict(
                next_url_path='next',
            ),
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items', [
                json_page([dict(id=1)], next='https://api.example.com/items/page/2'),
            ])
            m.get('https://api.example.com/items/page/2', [
                json_page([dict(id=2)], next=None),
            ])
            batches = list(source.load_data())

        self.assertEqual(batches, [[dict(id=1), dict(id=2)]])
        self.assertEqual(len(m.request_history), 2)

    def test_next_url_pagination_relative_url_and_duplicate_protection(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            query=dict(api_key='secret'),
            response_parser='data',
            pagination=dict(
                next_url_path='next',
            ),
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items', [
                # Relative next URL is resolved against the configured URL.
                json_page([dict(id=1)], next='/items/2'),
            ])
            m.get('https://api.example.com/items/2', [
                # Points back to the first URL: duplicate protection stops the loop.
                json_page([dict(id=2)], next='https://api.example.com/items'),
            ])
            batches = list(source.load_data())

        self.assertEqual(batches, [[dict(id=1), dict(id=2)]])
        self.assertEqual(len(m.request_history), 2)
        # The configured query params are only applied to the first request;
        # the next URL is followed as-is.
        self.assertEqual(m.request_history[0].qs.get('api_key'), ['secret'])
        self.assertNotIn('api_key', m.request_history[1].qs)

    def test_pagination_with_response_parser_and_columns(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            response_parser='response.items',
            columns=['name'],
            pagination=dict(type='page'),
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items', [
                dict(json=dict(response=dict(items=['a', 'b'])), status_code=200),
                dict(json=dict(response=dict(items=['c'])), status_code=200),
                dict(json=dict(response=dict(items=[])), status_code=200),
            ])
            batches = list(source.load_data())

        self.assertEqual(
            batches,
            [[dict(name='a'), dict(name='b'), dict(name='c')]],
        )

    def test_post_pagination_params_merged_into_payload(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            method='POST',
            payload=dict(filter='active'),
            pagination=dict(type='page'),
        ))

        with requests_mock.Mocker() as m:
            m.post('https://api.example.com/items', [
                json_page([dict(id=1)]),
                json_page([]),
            ])
            batches = list(source.load_data())

        self.assertEqual(batches, [[dict(id=1)]])
        body = parse_qs(m.request_history[0].text)
        self.assertEqual(body.get('filter'), ['active'])
        self.assertEqual(body.get('page'), ['1'])
        body = parse_qs(m.request_history[1].text)
        self.assertEqual(body.get('page'), ['2'])

    def test_pagination_with_retry_on_later_page(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            retry=dict(max_attempts=3, base_delay=0),
            pagination=dict(type='page'),
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items', [
                json_page([dict(id=1)]),
                dict(status_code=429, headers={'Retry-After': '0'}, json=dict(error='slow down')),
                json_page([dict(id=2)]),
                json_page([]),
            ])
            batches = list(source.load_data())

        self.assertEqual(batches, [[dict(id=1), dict(id=2)]])
        self.assertEqual(len(m.request_history), 4)

    def test_pagination_requires_json_response(self):
        source = Api(config=dict(
            url='https://api.example.com/items.csv',
            pagination=dict(type='page'),
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items.csv', text='id,name\n1,a\n')
            with self.assertRaises(Exception) as ctx:
                list(source.load_data())

        self.assertIn('only supported for JSON', str(ctx.exception))

    def test_invalid_pagination_type(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            pagination=dict(type='banana'),
        ))

        with self.assertRaises(ValueError):
            list(source.load_data())

    def test_discover_with_pagination_returns_single_stream(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            pagination=dict(type='page'),
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items', [
                json_page([dict(id=1, name='a')]),
                json_page([dict(id=2, name='b')]),
                json_page([]),
            ])
            catalog = source.discover()

        self.assertEqual(len(catalog.streams), 1)
        stream = catalog.streams[0]
        self.assertEqual(stream.stream, 'api')
        self.assertEqual(
            sorted(stream.schema.properties.keys()),
            ['id', 'name'],
        )


class ApiRetryTest(unittest.TestCase):
    def test_retry_on_429_then_success(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            retry=dict(max_attempts=3, base_delay=0),
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items', [
                dict(status_code=429, headers={'Retry-After': '0'}, json=dict(error='slow down')),
                json_page([dict(id=1)]),
            ])
            batches = list(source.load_data())

        self.assertEqual(batches, [[dict(id=1)]])
        self.assertEqual(len(m.request_history), 2)

    def test_retry_on_5xx_exhausted(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            retry=dict(max_attempts=3, base_delay=0),
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items', [
                dict(status_code=503, json=dict(error='boom')),
            ])
            with self.assertRaises(Exception) as ctx:
                source.test_connection()

        self.assertIn('503', str(ctx.exception))
        self.assertEqual(len(m.request_history), 3)

    def test_no_retry_on_client_error(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            retry=dict(max_attempts=5, base_delay=0),
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items', [
                dict(status_code=404, json=dict(error='not found')),
            ])
            with self.assertRaises(Exception):
                source.test_connection()

        self.assertEqual(len(m.request_history), 1)

    def test_retry_custom_statuses_disabled(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            retry=dict(max_attempts=5, base_delay=0, retry_statuses=[]),
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items', [
                dict(status_code=429, json=dict(error='slow down')),
            ])
            with self.assertRaises(Exception):
                source.test_connection()

        self.assertEqual(len(m.request_history), 1)

    def test_retry_exponential_backoff(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            retry=dict(
                max_attempts=4,
                base_delay=1,
                backoff_factor=2,
                max_delay=100,
                respect_retry_after=False,
            ),
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items', [
                dict(status_code=500, json=dict(error='a')),
                dict(status_code=502, json=dict(error='b')),
                dict(status_code=503, json=dict(error='c')),
                json_page([dict(id=1)]),
            ])
            with patch('mage_integrations.sources.api.time.sleep') as mock_sleep:
                batches = list(source.load_data())

        self.assertEqual(batches, [[dict(id=1)]])
        self.assertEqual(
            [call.args[0] for call in mock_sleep.call_args_list],
            [1, 2, 4],
        )

    def test_retry_backoff_respects_max_delay(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            retry=dict(
                max_attempts=4,
                base_delay=10,
                backoff_factor=2,
                max_delay=15,
                respect_retry_after=False,
            ),
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items', [
                dict(status_code=500, json=dict(error='a')),
                dict(status_code=500, json=dict(error='b')),
                dict(status_code=500, json=dict(error='c')),
                json_page([dict(id=1)]),
            ])
            with patch('mage_integrations.sources.api.time.sleep') as mock_sleep:
                list(source.load_data())

        self.assertEqual(
            [call.args[0] for call in mock_sleep.call_args_list],
            [10, 15, 15],
        )

    def test_retry_after_seconds_parsing(self):
        parse = Api._Api__retry_after_seconds

        self.assertEqual(parse(SimpleNamespace(headers={'Retry-After': '5'})), 5.0)
        self.assertEqual(parse(SimpleNamespace(headers={'Retry-After': '0'})), 0.0)
        self.assertIsNone(parse(SimpleNamespace(headers={})))
        self.assertIsNone(parse(SimpleNamespace(headers={'Retry-After': 'not-a-date'})))

        import time
        http_date = formatdate(time.time() + 30, usegmt=True)
        delay = parse(SimpleNamespace(headers={'Retry-After': http_date}))
        self.assertIsNotNone(delay)
        self.assertTrue(0 < delay <= 30)


class ApiExistingBehaviorTest(unittest.TestCase):
    def test_non_paginated_json_single_request(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            method='GET',
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items',
                  json=[dict(id=1, name='a'), dict(id=2, name='b')])
            batches = list(source.load_data())

        self.assertEqual(batches, [[dict(id=1, name='a'), dict(id=2, name='b')]])
        self.assertEqual(len(m.request_history), 1)

    def test_non_paginated_json_with_response_parser_and_columns(self):
        source = Api(config=dict(
            url='https://api.example.com/items',
            response_parser='response.items',
            columns=['name'],
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items',
                  json=dict(response=dict(items=['a', 'b'])))
            batches = list(source.load_data())

        self.assertEqual(batches, [[dict(name='a'), dict(name='b')]])
        self.assertEqual(len(m.request_history), 1)

    def test_csv_response_unchanged(self):
        source = Api(config=dict(
            url='https://api.example.com/items.csv',
            has_header=True,
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items.csv', text='id,name\n1,a\n2,b\n')
            batches = list(source.load_data())

        self.assertEqual(batches, [[dict(id=1, name='a'), dict(id=2, name='b')]])
        self.assertEqual(len(m.request_history), 1)

    def test_google_sheets_csv_unchanged(self):
        source = Api(config=dict(
            url='https://docs.google.com/spreadsheets/d/e/abc123/pub?output=csv',
            has_header=True,
        ))

        with requests_mock.Mocker() as m:
            m.get(
                'https://docs.google.com/spreadsheets/d/e/abc123/pub?output=csv',
                text='id,name\n1,a\n2,b\n',
            )
            batches = list(source.load_data())

        self.assertEqual(batches, [[dict(id=1, name='a'), dict(id=2, name='b')]])
        self.assertEqual(len(m.request_history), 1)

    def test_url_query_string_preserved_when_merging_query_config(self):
        source = Api(config=dict(
            url='https://api.example.com/items?api_key=secret',
            query=dict(format='json'),
        ))

        with requests_mock.Mocker() as m:
            m.get('https://api.example.com/items', json=[dict(id=1)])
            list(source.load_data())

        self.assertEqual(m.request_history[0].qs.get('api_key'), ['secret'])
        self.assertEqual(m.request_history[0].qs.get('format'), ['json'])


if __name__ == '__main__':
    unittest.main()
