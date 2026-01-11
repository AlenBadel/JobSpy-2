from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from datetime import date, datetime
import re

from jobspy.model import (
    JobPost,
    JobResponse,
    Scraper,
    ScraperInput,
    Site,
    JobType,
    Country,
    Compensation,
    CompensationInterval,
    Location,
)
from jobspy.util import create_session, create_logger

log = create_logger("LevelsFyi")

class LevelsFyi(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        site = Site(Site.LEVELSFYI)
        super().__init__(site, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent)
        self.session = None

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.session = create_session(
            proxies=self.proxies, ca_cert=self.ca_cert, has_retry=True
        )
        if self.user_agent:
            self.session.headers.update({"User-Agent": self.user_agent})

        job_list: list[JobPost] = []
        
        # Determine location slug
        location_slug = "united-states"
        if scraper_input.country and scraper_input.country.value:
             # Basic mapping, can be improved
             if scraper_input.country == Country.USA:
                 location_slug = "united-states"
             elif scraper_input.country == Country.CANADA:
                 location_slug = "canada"
             elif scraper_input.country == Country.UK:
                 location_slug = "united-kingdom"
             elif scraper_input.country == Country.INDIA:
                 location_slug = "india"
             else:
                 location_slug = scraper_input.country.value # fallback
        
        # If scraper_input.location is provided, we might want to use that instead of country
        # But Levels.fyi uses slugs like 'united-states', 'new-york-city', etc.
        # For now, we default to country-based scraping if location is broad. 
        # Ideally we'd search for the location slug.
        
        url = f"https://www.levels.fyi/jobs/location/{location_slug}"
        
        try:
            response = self.session.get(url)
            if response.status_code != 200:
                log.error(f"LevelsFyi request failed with status code {response.status_code}")
                return JobResponse(jobs=[])
            
             # Extract __NEXT_DATA__
            match = re.search(r'__NEXT_DATA__" type="application/json">(.*?)</script>', response.text)
            if not match:
                log.error("Could not find __NEXT_DATA__ in response")
                return JobResponse(jobs=[])
            
            data = json.loads(match.group(1))
            
            results = data.get("props", {}).get("pageProps", {}).get("initialJobsData", {}).get("results", [])
            
            for company_data in results:
                company_name = company_data.get("companyName")
                company_url = f"https://www.levels.fyi/companies/{company_data.get('companySlug')}/jobs" if company_data.get('companySlug') else None
                company_logo = company_data.get("companyIcon")
                
                for job in company_data.get("jobs", []):
                    title = job.get("title")
                    job_id = job.get("id")
                    job_url = job.get("applicationUrl")
                    description = None 
                    
                    locations = job.get("locations", [])
                    location_obj = None
                    if locations:
                        loc_str = locations[0]
                        parts = [p.strip() for p in loc_str.split(",")]
                        country = None
                        state = None
                        city = None
                        
                        if len(parts) >= 3:
                            country = parts[-1]
                            state = parts[-2]
                            city = parts[-3]
                        elif len(parts) == 2:
                            state = parts[-1]
                            city = parts[0]
                        elif len(parts) == 1:
                            city = parts[0]
                        
                        location_obj = Location(city=city, state=state, country=country)
                        
                    date_posted_str = job.get("postingDate")
                    date_posted = None
                    if date_posted_str:
                         try:
                             date_posted = datetime.fromisoformat(date_posted_str.replace("Z", "+00:00")).date()
                         except (ValueError, TypeError):
                             date_posted = None
                    
                    # Compensation
                    compensation = None
                    if job.get("minBaseSalary") and job.get("maxBaseSalary"):
                         compensation = Compensation(
                             interval=CompensationInterval.YEARLY,
                             min_amount=job.get("minBaseSalary"),
                             max_amount=job.get("maxBaseSalary"),
                             currency=job.get("baseSalaryCurrency", "USD")
                         )
                    
                    job_type_list = []
                    is_remote = False
                    work_arrangement = job.get("workArrangement")
                    if work_arrangement == "remote":
                        is_remote = True
                    elif work_arrangement == "hybrid":
                        # JobType doesn't have HYBRID either usually, checking logic
                        pass
                    
                    job_post = JobPost(
                        id=job_id,
                        title=title,
                        company_name=company_name,
                        company_url=company_url,
                        company_logo=company_logo,
                        job_url=job_url,
                        location=location_obj,
                        date_posted=date_posted,
                        compensation=compensation,
                        job_type=job_type_list if job_type_list else None,
                        is_remote=is_remote,
                        description=description
                    )
                    job_list.append(job_post)
            
            # Since we can't paginate easily yet, we just return the initial jobs.
            # Warning: this might be fewer than results_wanted.
            if len(job_list) < scraper_input.results_wanted:
                log.warning(f"Could only retrieve {len(job_list)} jobs due to pagination limitations on Levels.fyi")
                
        except Exception as e:
            log.error(f"LevelsFyi scrape error: {e}")
            
        return JobResponse(jobs=job_list)
