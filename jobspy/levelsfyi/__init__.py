from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import re
from markdownify import markdownify as md

from jobspy.model import (
    JobPost,
    JobResponse,
    Scraper,
    ScraperInput,
    Site,
    Country,
    Compensation,
    CompensationInterval,
    Location,
)
from jobspy.util import create_session, create_logger

log = create_logger("LevelsFyi")


class LevelsFyi(Scraper):
    """
    Levels.fyi Job Scraper
    
    Strategy:
    1. Fetch the main jobs page to get currently hiring companies
    2. For each company, visit their dedicated jobs page to get all their listings
    3. Use popular companies list as additional source if needed
    4. Collect jobs until reaching results_wanted
    
    Note: Levels.fyi uses client-side pagination, so server requests only return
    the first page of results (~15 jobs per company page). We maximize coverage 
    by visiting multiple company pages.
    """
    
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
        seen_ids: set[str] = set()
        
        # Determine locale based on country
        locale = self._get_locale(scraper_input)
        
        # Step 1: Get companies from main jobs page
        log.info(f"Fetching main jobs page with locale: {locale}")
        companies, initial_jobs = self._get_companies_from_main_page(locale)
        
        # Add initial jobs from main page
        for job in initial_jobs:
            if job.id and job.id not in seen_ids:
                seen_ids.add(job.id)
                job_list.append(job)
        
        log.info(f"Found {len(companies)} companies and {len(job_list)} initial jobs from main page")
        
        # Step 2: If we need more jobs, fetch from each company page
        if len(job_list) < scraper_input.results_wanted:
            log.info("Fetching jobs from individual company pages...")
            
            # Get popular companies as additional sources
            #popular_slugs = self._get_popular_company_slugs()
            popular_slugs = []
            
            # Combine company slugs (main page companies first, then popular)
            all_slugs = [c["slug"] for c in companies if c.get("slug")]
            for slug in popular_slugs:
                if slug not in all_slugs:
                    all_slugs.append(slug)
            
            log.info(f"Total company slugs to check: {len(all_slugs)}")
            
            # Fetch jobs from company pages concurrently
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {}
                for slug in all_slugs:
                    if len(job_list) >= scraper_input.results_wanted:
                        break
                    future = executor.submit(self._fetch_company_jobs, slug, locale)
                    futures[future] = slug
                
                for future in as_completed(futures):
                    if len(job_list) >= scraper_input.results_wanted:
                        break
                    
                    slug = futures[future]
                    try:
                        company_jobs = future.result()
                        new_count = 0
                        for job in company_jobs:
                            if job.id and job.id not in seen_ids:
                                seen_ids.add(job.id)
                                job_list.append(job)
                                new_count += 1
                                if len(job_list) >= scraper_input.results_wanted:
                                    break
                        
                        if new_count > 0:
                            log.debug(f"Added {new_count} new jobs from {slug}")
                    except Exception as e:
                        log.error(f"Error fetching jobs for {slug}: {e}")
        
        # Step 3: Fetch descriptions if requested
        if scraper_input.linkedin_fetch_description:
            log.info(f"Fetching descriptions for {len(job_list)} jobs...")
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {executor.submit(self._fetch_description, job): job for job in job_list}
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as exc:
                        log.error(f"Description fetch error: {exc}")
        
        # Trim to results_wanted
        job_list = job_list[:scraper_input.results_wanted]
        
        log.info(f"Returning {len(job_list)} jobs")
        return JobResponse(jobs=job_list)

    def _get_locale(self, scraper_input: ScraperInput) -> str:
        """Determine the locale based on country"""
        if scraper_input.country:
            country_locale_map = {
                Country.USA: "en-us",
                Country.CANADA: "en-ca",
                Country.UK: "en-gb",
                Country.INDIA: "en-in",
                Country.GERMANY: "de-de",
                Country.FRANCE: "fr-fr",
            }
            return country_locale_map.get(scraper_input.country, "en-us")
        return "en-us"
    
    def _get_companies_from_main_page(self, locale: str) -> tuple[list[dict], list[JobPost]]:
        """Fetch the main jobs page and extract companies and preview jobs"""
        url = f"https://www.levels.fyi/{locale}/jobs"
        
        try:
            response = self.session.get(url)
            if response.status_code != 200:
                log.error(f"Main page request failed: {response.status_code}")
                return [], []
            
            data = self._extract_next_data(response.text)
            if not data:
                return [], []
            
            results = data.get("props", {}).get("pageProps", {}).get("initialJobsData", {}).get("results", [])
            
            companies = []
            jobs = []
            
            for company_data in results:
                company_name = company_data.get("companyName")
                company_slug = company_data.get("companySlug")
                company_logo = company_data.get("companyIcon")
                
                companies.append({
                    "name": company_name,
                    "slug": company_slug,
                    "logo": company_logo,
                })
                
                for job in company_data.get("jobs", []):
                    job_post = self._parse_job(job, company_name, company_slug, company_logo)
                    if job_post:
                        jobs.append(job_post)
            
            return companies, jobs
            
        except Exception as e:
            log.error(f"Error fetching main page: {e}")
            return [], []
    
    def _get_popular_company_slugs(self) -> list[str]:
        """Get list of popular company slugs from /companies page"""
        try:
            response = self.session.get("https://www.levels.fyi/companies")
            if response.status_code != 200:
                return []
            
            data = self._extract_next_data(response.text)
            if not data:
                return []
            
            page_props = data.get("props", {}).get("pageProps", {})
            popular = page_props.get("popularCompanies", [])
            
            return [c.get("slug") for c in popular if c.get("slug")]
            
        except Exception as e:
            log.error(f"Error fetching popular companies: {e}")
            return []
    
    def _fetch_company_jobs(self, company_slug: str, locale: str) -> list[JobPost]:
        """Fetch all jobs for a specific company"""
        url = f"https://www.levels.fyi/{locale}/jobs/company/{company_slug}"
        
        try:
            response = self.session.get(url)
            if response.status_code != 200:
                return []
            
            data = self._extract_next_data(response.text)
            if not data:
                return []
            
            results = data.get("props", {}).get("pageProps", {}).get("initialJobsData", {}).get("results", [])
            
            jobs = []
            for company_data in results:
                company_name = company_data.get("companyName")
                company_logo = company_data.get("companyIcon")
                
                for job in company_data.get("jobs", []):
                    job_post = self._parse_job(job, company_name, company_slug, company_logo)
                    if job_post:
                        jobs.append(job_post)
            
            return jobs
            
        except Exception as e:
            log.error(f"Error fetching company {company_slug}: {e}")
            return []
    
    def _parse_job(self, job: dict, company_name: str, company_slug: str, company_logo: str | None) -> JobPost | None:
        """Parse a job dict into a JobPost object"""
        try:
            job_id = str(job.get("id"))
            title = job.get("title")
            job_url = job.get("applicationUrl")
            
            # Parse location
            location_obj = None
            locations = job.get("locations", [])
            if locations:
                loc_str = locations[0]
                parts = [p.strip() for p in loc_str.split(",")]
                
                city = state = country = None
                if len(parts) >= 3:
                    city, state, country = parts[-3], parts[-2], parts[-1]
                elif len(parts) == 2:
                    city, state = parts[0], parts[-1]
                elif len(parts) == 1:
                    city = parts[0]
                
                location_obj = Location(city=city, state=state, country=country)
            
            # Parse date
            date_posted = None
            date_str = job.get("postingDate")
            if date_str:
                try:
                    date_posted = datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
                except (ValueError, TypeError):
                    pass
            
            # Parse compensation
            compensation = None
            min_salary = job.get("minBaseSalary")
            max_salary = job.get("maxBaseSalary")
            if min_salary and max_salary:
                compensation = Compensation(
                    interval=CompensationInterval.YEARLY,
                    min_amount=min_salary,
                    max_amount=max_salary,
                    currency=job.get("baseSalaryCurrency", "USD")
                )
            
            # Work arrangement
            is_remote = job.get("workArrangement") == "remote"
            
            company_url = f"https://www.levels.fyi/companies/{company_slug}/jobs" if company_slug else None
            
            return JobPost(
                id=job_id,
                title=title,
                company_name=company_name,
                company_url=company_url,
                company_logo=company_logo,
                job_url=job_url,
                location=location_obj,
                date_posted=date_posted,
                compensation=compensation,
                is_remote=is_remote,
                description=None  # Fetched separately if requested
            )
            
        except Exception as e:
            log.error(f"Error parsing job: {e}")
            return None

    def _fetch_description(self, job: JobPost) -> None:
        """Fetch full description for a job from its detail page"""
        if not job.id:
            return
        
        detail_url = f"https://www.levels.fyi/jobs?jobId={job.id}"
        
        try:
            resp = self.session.get(detail_url)
            if resp.status_code != 200:
                return
            
            data = self._extract_next_data(resp.text)
            if not data:
                return
            
            props = data.get("props", {}).get("pageProps", {})
            job_details = props.get("initialJobDetails") or props.get("job") or props.get("initialJob")
            
            if job_details:
                desc_html = job_details.get("jobDescription") or job_details.get("description")
                if desc_html:
                    job.description = md(desc_html).strip()
                    
        except Exception as e:
            log.error(f"Error fetching description for {job.id}: {e}")

    def _extract_next_data(self, html: str) -> dict | None:
        """Extract __NEXT_DATA__ JSON from HTML"""
        match = re.search(r'__NEXT_DATA__" type="application/json">(.*?)</script>', html)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                return None
        return None

