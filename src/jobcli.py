import os
import argparse
from .job_summarizer import JobSummarizer
from config.settings import JOB_CONFIG, LLM_CONFIG, OUTPUT_DIR
from jobspy import scrape_jobs

def main():
    parser = argparse.ArgumentParser(description='Job summary generator')
    parser.add_argument('--output-dir', type=str, default=OUTPUT_DIR, help='Output directory')
    
    args = parser.parse_args()
    
    
    # Initialize client
    job_summarizer = JobSummarizer(LLM_CONFIG['api_key'], LLM_CONFIG.get('model'))
        
    # Fetch jobs
    jobs = scrape_jobs(**JOB_CONFIG)
    jobs = jobs.sort_values(by='date_posted', ascending=False)
    jobs = jobs.to_dict(orient="records")
    if jobs:
        print(f"Fetched {len(jobs)} jobs from providers.")
    else:
        print("No jobs were fetched. Rebuilding feeds from existing data.")
    
    # Generate summaries
    output_file = os.path.join(args.output_dir, "jobsDaily.json")
    
    # Create output directory
    os.makedirs(args.output_dir, exist_ok=True)
    
    # Generate summaries and save
    try:
        success = job_summarizer.summarize_jobs(jobs, output_file)
        if success:
            print(f"JSON feed generated and saved to: {output_file}")
        else:
            print(f"Feed generation had errors; results may be incomplete: {output_file}")
    except Exception as e:
        print(f"Error generating summary: {e}")
        success = False
    
    # Only report success if summary is complete
    if success:
        print("Summary generated successfully.")
    else:
        print("Summary incomplete or failed.")

if __name__ == '__main__':
    main()
