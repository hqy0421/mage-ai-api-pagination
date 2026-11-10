# API

List of sample APIs: https://apipheny.io/free-api/

## Config

You must enter the following credentials when configuring this source:

| Key | Description | Required or optional |
| --- | --- | --- |
| `url` | API URL | Required |
| `method` | `GET` or `POST` | Optional: `GET` is default |
| `query` | URL query parameters. | Optional |
| `payload` | When method is `POST`, this payload is used in the request body. | Optional |
| `headers` | Request headers. | Optional |
| `response_parser` | Parse the API response using dot notation. The final result must be an array. | Optional |
| `columns` | If the final data returned from the API or from the `response_parser` is not a JSON object (e.g. it’s an array of strings or an array of array of strings), then you must define the columns. | Required (conditionally) |
|`separator`| If the file extension is TSV, XLSX (or CSV with specific separator), then you can define it | Optional: ',' is default|
|`has_header`| If the file extension is TSV, XLSX or CSV, and contains a header, then you can define it as True | Optional: False is default|
|`timeout`| Request timeout in seconds. | Optional: `12` is default|
|`pagination`| Fetch all pages of a JSON API and merge the records. See [Pagination](#pagination). | Optional: no pagination by default|
|`retry`| Retry requests that fail with `429` or `5xx` status codes, using `Retry-After` or exponential backoff. See [Retries](#retries). | Optional: retries enabled with defaults|

<br />

## Pagination

Add a `pagination` block to the config to fetch every page of a JSON API response.
The records of all pages are merged into a single result. Pagination only applies to
JSON responses; CSV, TSV, XLSX and Google Sheets responses are always fetched with a
single request.

| Key | Description | Default |
| --- | --- | --- |
| `type` | One of `page`, `offset`, `cursor`, `next_url`. Auto-detected from the other keys when omitted. | `page` |
| `max_pages` | Maximum number of pages to fetch. Stops the sync even if more pages are available. | `100` |
| `stop_when_empty` | Stop when a page returns no records. | `true` |

### `page`

| Key | Description | Default |
| --- | --- | --- |
| `page_param` | Name of the query parameter that holds the page number. | `page` |
| `first_page` | Number of the first page to fetch. | `1` |
| `page_size_param` | Name of the query parameter that holds the page size. Sent on every request when set together with `page_size`. | _None_ |
| `page_size` | Number of records per page. | _None_ |
| `total_pages_path` | Dot-notation path to the total number of pages in the response body (e.g. `meta.total_pages`). Stops pagination once reached. | _None_ |

```yaml
url: https://api.example.com/users
response_parser: data
pagination:
  type: page
  page_param: page
  first_page: 1
  page_size_param: per_page
  page_size: 100
  total_pages_path: meta.total_pages
```

### `offset`

| Key | Description | Default |
| --- | --- | --- |
| `offset_param` | Name of the query parameter that holds the offset. | `offset` |
| `offset_start` | Offset of the first request. | `0` |
| `limit_param` | Name of the query parameter that holds the page size. | `limit` |
| `limit` | Number of records per page. The offset is increased by this value after every page. | `100` |
| `total_count_path` | Dot-notation path to the total number of records in the response body (e.g. `meta.total`). Stops pagination once the offset reaches it. | _None_ |

```yaml
url: https://api.example.com/users
response_parser: data
pagination:
  type: offset
  offset_param: offset
  limit_param: limit
  limit: 100
  total_count_path: meta.total
```

### `cursor`

| Key | Description | Default |
| --- | --- | --- |
| `cursor_param` | Name of the query parameter that holds the cursor. | `cursor` |
| `cursor_path` | Dot-notation path to the next cursor in the response body (e.g. `meta.next_cursor`). Pagination stops when the value is missing or empty. | Required for `cursor` |
| `cursor_start` | Cursor value to send on the first request. The parameter is omitted when not set. | _None_ |

```yaml
url: https://api.example.com/users
response_parser: data
pagination:
  type: cursor
  cursor_param: cursor
  cursor_path: meta.next_cursor
```

### `next_url`

| Key | Description | Default |
| --- | --- | --- |
| `next_url_path` | Dot-notation path to the URL of the next page in the response body (e.g. `next`). Relative URLs are resolved against the configured `url`. Pagination stops when the value is missing or empty. | Required for `next_url` |

```yaml
url: https://api.example.com/users
response_parser: data
pagination:
  type: next_url
  next_url_path: links.next
```

For all types, pagination also stops when `max_pages` is reached or when the API
returns a cursor or URL that was already requested (duplicate cursor/URL protection
against infinite loops).

For `GET` requests the pagination parameters are added to the URL query string.
For `POST` requests they are merged into the request body when the payload is a
dictionary, and into the URL query string otherwise.

<br />

## Retries

Requests that fail with a `429` or `5xx` status code are retried automatically.
When the response contains a `Retry-After` header (seconds or HTTP-date) it is
honored; otherwise an exponential backoff delay is used. This applies to every
request, including each page of a paginated sync.

| Key | Description | Default |
| --- | --- | --- |
| `max_attempts` | Total number of attempts per request, including the first one. Set to `1` to disable retries. | `5` |
| `base_delay` | Delay in seconds before the first retry. | `1.0` |
| `backoff_factor` | Multiplier applied to the delay after every attempt. | `2.0` |
| `max_delay` | Maximum computed backoff delay in seconds. | `60.0` |
| `respect_retry_after` | Whether to honor the `Retry-After` response header. | `true` |
| `retry_statuses` | List of HTTP status codes that trigger a retry. | `[429, 500, 502, 503, 504]` |

```yaml
url: https://api.example.com/users
retry:
  max_attempts: 5
  base_delay: 1
  backoff_factor: 2
  max_delay: 60
  respect_retry_after: true
  retry_statuses: [429, 500, 502, 503, 504]
```

<br />

### Example GET API 1

```json
{
  "url": "https://api.plos.org/search",
  "query": {
    "q": "title:DNA"
  },
  "headers": {
    "Content-Type": "application/json"
  },
  "response_parser": "response.docs[0].author_display",
  "columns": ["author_display"]
}
```

The response from the above endpoint is:

```json
{
  "response": {
    "numFound": 5669,
    "start": 0,
    "maxScore": 6.7217336,
    "docs": [
      {
        "id": "10.1371/journal.pone.0000290",
        "journal": "PLoS ONE",
        "eissn": "1932-6203",
        "publication_date": "2007-03-14T00:00:00Z",
        "article_type": "Research Article",
        "author_display": [
          "Rayna I. Kraeva",
          "Dragomir B. Krastev",
          "Assen Roguev",
          "Anna Ivanova",
          "Marina N. Nedelcheva-Veleva",
          "Stoyno S. Stoynov"
        ],
        "abstract": [
          "Nucleic acids, due to their structural and chemical properties, can form double-stranded secondary structures that assist the transfer of genetic information and can modulate gene expression. However, the nucleotide sequence alone is insufficient in explaining phenomena like intron-exon recognition during RNA processing. This raises the question whether nucleic acids are endowed with other attributes that can contribute to their biological functions. In this work, we present a calculation of thermodynamic stability of DNA/DNA and mRNA/DNA duplexes across the genomes of four species in the genus Saccharomyces by nearest-neighbor method. The results show that coding regions are more thermodynamically stable than introns, 3′-untranslated regions and intergenic sequences. Furthermore, open reading frames have more stable sense mRNA/DNA duplexes than the potential antisense duplexes, a property that can aid gene discovery. The lower stability of the DNA/DNA and mRNA/DNA duplexes of 3′-untranslated regions and the higher stability of genes correlates with increased mRNA level. These results suggest that the thermodynamic stability of DNA/DNA and mRNA/DNA duplexes affects mRNA transcription."
        ],
        "title_display": "Stability of mRNA/DNA and DNA/DNA Duplexes Affects mRNA Transcription",
        "score": 6.7217336
      }
    ]
  }
}
```

However, with the `response_parser` value of `response.docs[0].author_display`,
the data that is extracted from the API’s response is:

```json
[
  "Rayna I. Kraeva",
  "Dragomir B. Krastev",
  "Assen Roguev",
  "Anna Ivanova",
  "Marina N. Nedelcheva-Veleva",
  "Stoyno S. Stoynov"
]
```

Since each item in the final data is not a JSON object, the `columns` configuration value is required.

The final data is converted into a JSON object before being outputted to its destination:

```json
[
  {
    "author_display": "Rayna I. Kraeva"
  },
  {
    "author_display": "Dragomir B. Krastev"
  },
  {
    "author_display": "Assen Roguev"
  },
  {
    "author_display": "Anna Ivanova"
  },
  {
    "author_display": "Marina N. Nedelcheva-Veleva"
  },
  {
    "author_display": "Stoyno S. Stoynov"
  }
]
```

<br />

### Example GET API 2

```json
{
  "url": "https://api.coingecko.com/api/v3/coins/markets",
  "query": {
    "vs_currency": "usd"
  }
}
```

The response from the above endpoint is:

```json
[
  {
    "id": "bitcoin",
    "symbol": "btc",
    "name": "Bitcoin",
    "image": "https://assets.coingecko.com/coins/images/1/large/bitcoin.png?1547033579",
    "current_price": 17154.23,
    "market_cap": 329331527834,
    "market_cap_rank": 1,
    "fully_diluted_valuation": 359638803581,
    "total_volume": 14339246353,
    "high_24h": 17227.56,
    "low_24h": 17107.75,
    "price_change_24h": 18.57,
    "price_change_percentage_24h": 0.10839,
    "market_cap_change_24h": -425260465.15130615,
    "market_cap_change_percentage_24h": -0.12896,
    "circulating_supply": 19230300.0,
    "total_supply": 21000000.0,
    "max_supply": 21000000.0,
    "ath": 69045,
    "ath_change_percentage": -75.16662,
    "ath_date": "2021-11-10T14:24:11.849Z",
    "atl": 67.81,
    "atl_change_percentage": 25185.95387,
    "atl_date": "2013-07-06T00:00:00.000Z",
    "roi": null,
    "last_updated": "2022-12-11T00:32:01.695Z"
  }
]
```

Because there is no `response_parser`, the final data matches the exact response from the API.

Since each item in the final data is a JSON object, the `columns` configuration value isn’t required.

<br />

### Example GET API with pagination

```yaml
url: https://api.example.com/users
headers:
  Authorization: Bearer token123
response_parser: data
pagination:
  type: cursor
  cursor_param: cursor
  cursor_path: meta.next_cursor
  max_pages: 50
retry:
  max_attempts: 5
```

With a response like:

```json
{
  "data": [
    {"id": 1, "name": "Urza"}
  ],
  "meta": {
    "next_cursor": "eyJpZCI6MX0="
  }
}
```

the source follows `meta.next_cursor` from page to page
(`?cursor=eyJpZCI6MX0=`, …), merges the `data` arrays of all pages into a single
set of records, and stops when the cursor is missing, repeated, or after 50 pages.

<br />

### Example POST API

```yaml
url: https://api.something.com/users
method: POST
payload:
  user:
    first_name: Urza
    power: 10
headers:
  "Content-Type": "application/json"
  token: abc123
response_parser: "user"
```

<br />


### Example XLSX or CSV

```yaml
url: https://docs.google.com/spreadsheets/d/e/2PACX-1vTLcLUBAJAWf-8NQSjlbB3E4LR6DWk5QIZC-KtRb1j2CXXcgY6mE6vOJAW8PoJ1BAOgjXYpE4tY1LAD/pub?output=xlsx
method: GET
has_header: True
```
The response from the above endpoint is:

```
teste,first_name,second_name,email
1,Sadella,Tythacott,stythacott0@sina.com.cn
2,Melessa,Flaune,mflaune1@si.edu
3,Caroljean,Filipowicz,cfilipowicz2@guardian.co.uk
4,Doll,Wannan,dwannan3@people.com.cn
5,Nancy,Giraudy,ngiraudy4@pagesperso-orange.fr
6,Dominic,Bimson,dbimson5@vinaora.com
7,Kikelia,Bishopp,kbishopp6@cdc.gov
8,Andrus,Pomfrett,apomfrett7@wikipedia.org
9,Wildon,Fillingham,wfillingham8@google.co.uk
10,Alfonse,Leechman,aleechman9@jalbum.net
11,Phil,Emblem,pemblema@opera.com
12,Eyde,Brewer,ebrewerb@istockphoto.com
....
```
Data can also be checked by clicking on the "Load Sample data" button.
