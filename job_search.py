from jobspy import scrape_jobs
import pandas as pd

jobs = scrape_jobs(
    site_name=["indeed", "linkedin", "naukri"],
    search_term="software engineer",
    location="Hyderabad, Telangana, India",
    results_wanted=10,
    hours_old=72,
    country_indeed="India",
    verbose=2
)

print(f"\nFound {len(jobs)} jobs")

if not jobs.empty:
    print(jobs[["site", "title", "company", "location", "job_url"]].to_string(index=False))

    jobs.to_excel("jobs.xlsx", index=False)
    jobs.to_csv("jobs.csv", index=False)

    print("\nSaved:")
    print("C:\\JobSpy\\jobs.xlsx")
    print("C:\\JobSpy\\jobs.csv")
else:
    print("No jobs found.")