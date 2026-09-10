# JobSpy → Flask + SQLite REST API

Built around the official JobSpy repository:

https://github.com/speedyapply/JobSpy

JobSpy is used only for the scraping layer. Flask exposes REST APIs, SQLite stores users/history/jobs/files, and JWT protects private endpoints.

## Features

- Signup API
- Login API
- JWT authentication
- Profile GET/UPDATE API
- Job scraping API using `jobspy.scrape_jobs()`
- Search history API
- Scraped jobs history API
- Exported CSV/JSON file history API
- Authenticated file download API
- SQLite database
- CORS
- Password hashing
- Per-user history isolation

## Project structure

```text
jobspy_flask_sqlite_api/
├── app.py
├── requirements.txt
├── .env.example
├── README.md
├── postman_collection.json
├── jobspy.db                 # created automatically
└── exports/                  # CSV/JSON files created by scraping
```

## Windows setup

Your current Python 3.10 installation works with the JobSpy package already installed.

From this project folder:

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

If you already installed `python-jobspy` globally, the virtual environment is still recommended for this project.

Set a JWT secret:

```cmd
set JWT_SECRET_KEY=use-a-long-random-secret-here
```

Start:

```cmd
python app.py
```

Open:

http://127.0.0.1:5000/

## Authentication

### Signup

`POST /api/auth/signup`

```json
{
  "name": "Vamsi",
  "email": "vamsi@example.com",
  "password": "StrongPassword123"
}
```

### Login

`POST /api/auth/login`

```json
{
  "email": "vamsi@example.com",
  "password": "StrongPassword123"
}
```

Both return:

```json
{
  "access_token": "JWT...",
  "user": {}
}
```

Use the token on protected APIs:

```text
Authorization: Bearer YOUR_ACCESS_TOKEN
```

## Profile API

### Get profile

`GET /api/profile`

### Update profile

`PUT /api/profile`

```json
{
  "name": "Vamsi Umesh",
  "headline": "B.Tech IT Graduate",
  "location": "Visakhapatnam, India",
  "skills": ["Python", "SQL", "ServiceNow", "Flutter"],
  "profile_image_url": ""
}
```

## Scraping API

### Main endpoint

`POST /api/scraping/jobs`

Example:

```json
{
  "search_term": "associate software engineer",
  "location": "Hyderabad, Telangana, India",
  "sites": ["indeed", "linkedin", "naukri"],
  "results_wanted": 10,
  "hours_old": 72,
  "country_indeed": "India"
}
```

A successful request:

1. Calls JobSpy.
2. Stores the search in `search_history`.
3. Stores every returned job in `job_results`.
4. Writes a CSV export.
5. Writes a JSON export.
6. Stores both file locations in `files`.
7. Returns the jobs and file IDs.

Generic alias:

`POST /api/scrape`

## Supported JobSpy sites

The API accepts:

```text
linkedin
indeed
glassdoor
google
zip_recruiter
bayt
naukri
bdjobs
```

The exact support and parameters come from JobSpy. See the upstream README for the current list and limitations:

https://github.com/speedyapply/JobSpy

For example, JobSpy documents `results_wanted` as the number of results per requested site and documents `hours_old` for filtering by posting age.

### Important JobSpy limitation

JobSpy documents that Indeed allows only one of these filter groups in a single search:

- `hours_old`
- `job_type` + `is_remote`
- `easy_apply`

LinkedIn similarly has limitations around `hours_old` and `easy_apply`.

The API does not pretend these limitations do not exist; it passes the supported parameters to JobSpy.

## History APIs

### Everything

`GET /api/history`

Returns:

- search history
- jobs belonging to each search
- file locations belonging to each search
- all user export files

### Searches only

`GET /api/history/searches`

### Files only

`GET /api/history/files`

### Jobs from one search

`GET /api/history/jobs/<search_id>`

## File API

`GET /api/files/<file_id>`

Downloads the CSV/JSON file belonging to the authenticated user.

The API verifies ownership before allowing download.

## SQLite tables

### users

Stores account/profile data and password hashes.

### search_history

Stores:

- user
- search term
- location
- sites
- filters
- result count
- export file IDs
- timestamp

### job_results

Stores the normalized important job fields plus the complete returned JobSpy record as JSON.

### files

Stores:

- owner
- search ID
- filename
- absolute server-side path
- type
- size
- timestamp

## Security notes

This is a development-ready API foundation, not a production deployment.

Before deploying publicly:

- Use a strong random JWT secret.
- Put secrets in environment variables.
- Run behind HTTPS.
- Add rate limiting.
- Add request validation.
- Consider refresh tokens and token revocation.
- Restrict CORS to your frontend domain.
- Do not expose the SQLite database file.
- Review the terms/rules of every job board you scrape.
- Expect some job boards to block or change their scraping behavior.

## Upstream JobSpy status

JobSpy is an actively changing scraper. The upstream repository documents the supported parameters and job boards. Changes on LinkedIn/other sites can affect scraping without changes to this Flask API.

Repository:

https://github.com/speedyapply/JobSpy
