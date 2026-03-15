import os
import argparse
from .arxiv_client import ArxivClient
from .paper_summarizer import PaperSummarizer
from config.settings import SEARCH_CONFIG, CATEGORIES, QUERY, LLM_CONFIG, OUTPUT_DIR, LAST_RUN_FILE

def main():
    parser = argparse.ArgumentParser(description='ArXiv paper summary generator')
    parser.add_argument('--query', type=str, default=QUERY, help='Search keywords')
    parser.add_argument('--categories', nargs='+', default=CATEGORIES, help='arXiv categories')
    parser.add_argument('--max-results', type=int, default=SEARCH_CONFIG['max_total_results'], help='Number of papers to fetch')
    parser.add_argument('--output-dir', type=str, default=OUTPUT_DIR, help='Output directory')
    
    args = parser.parse_args()
    
    # Update config
    SEARCH_CONFIG['max_total_results'] = args.max_results
    
    # Initialize client
    arxiv_client = ArxivClient(SEARCH_CONFIG)
    paper_summarizer = PaperSummarizer(LLM_CONFIG['api_key'], LLM_CONFIG.get('model'))
    
    # Prepare last_run_file path
    if LAST_RUN_FILE:
        last_run_file = os.path.join(args.output_dir, LAST_RUN_FILE)
    else:
        last_run_file = False
    
    # Fetch papers
    papers = arxiv_client.search_papers(
        categories=args.categories, 
        query=args.query,
        last_run_file=last_run_file
    )
    if papers:
        print(f"Fetched {len(papers)} papers from arXiv.")
    else:
        print("No newly fetched papers. Rebuilding feeds from existing data.")
    
    # Record latest entry ID for saving after successful summary
    latest_entry_id = papers[0]['entry_id'] if papers else None
    
    # Generate summaries
    output_file = os.path.join(args.output_dir, "arXivDaily.json")
    
    # Create output directory
    os.makedirs(args.output_dir, exist_ok=True)
    
    # Generate summaries and save
    try:
        success = paper_summarizer.summarize_papers(papers, output_file)
        if success:
            print(f"JSON feed generated and saved to: {output_file}")
        else:
            print(f"Feed generation had errors; results may be incomplete: {output_file}")
    except Exception as e:
        print(f"Error generating summary: {e}")
        success = False
    
    # Only save latest entry ID after successful summary
    if success and latest_entry_id and last_run_file:
        arxiv_client.save_last_run_info(latest_entry_id, last_run_file, len(papers))
        print(f"Summary succeeded. Updated run record. Next run will start from entry ID: {latest_entry_id}")
    elif success and not latest_entry_id:
        print("No new arXiv entries were fetched. Existing feeds were refreshed without advancing the run record.")
    else:
        print("Summary incomplete or failed. Run record not updated; next run will retry these papers.")

if __name__ == '__main__':
    main()
