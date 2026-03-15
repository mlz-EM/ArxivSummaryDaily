"""
ArXiv API client module.
"""
import arxiv
import json
import os
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from pathlib import Path
from config.settings import SEARCH_CONFIG, QUERY

class ArxivClient:
    def __init__(self, config=None):
        self.client = arxiv.Client()
        self.config = config or SEARCH_CONFIG

    def _safe_get_categories(self, paper: arxiv.Result) -> List[str]:
        """Safely extract paper categories."""
        try:
            if isinstance(paper.categories, (list, tuple, set)):
                return list(paper.categories)
            elif isinstance(paper.categories, str):
                return [paper.categories]
            else:
                return [str(paper.categories)]
        except Exception as e:
            print(f"Debug - error getting categories: {e}")
            return [paper.primary_category] if paper.primary_category else []

    def _load_last_run_info(self, last_run_file: str) -> Optional[str]:
        """Load the latest entry ID from the last run."""
        try:
            with open(last_run_file, 'r') as f:
                data = json.load(f)
                return data.get('latest_entry_id')
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def save_last_run_info(self, latest_entry_id: str, last_run_file: str, total_results: int = 0):
        """
        Save the latest entry ID for this run.
        
        Args:
            latest_entry_id: Latest entry ID
            last_run_file: File path to store run info
            total_results: Number of results fetched in this run
        """
        try:
            os.makedirs(os.path.dirname(last_run_file), exist_ok=True)
            with open(last_run_file, 'w') as f:
                json.dump({
                    'latest_entry_id': latest_entry_id,
                    'timestamp': datetime.now().isoformat(),
                    'total_results': total_results
                }, f, indent=2)
            print(f"Run record updated, latest entry ID: {latest_entry_id}")
        except Exception as e:
            print(f"Error saving run record: {e}")

    def save_results(self, results: List[Dict[str, Any]], output_dir: str, metadata_file: str):
        """
        Save search results to JSON, merge with existing results, and purge expired records.
        
        Args:
            results: Search results list
            output_dir: Output directory
            metadata_file: Metadata file name
        """
        metadata_path = os.path.join(output_dir, metadata_file)
        
        # Load existing results
        existing_results = []
        if os.path.exists(metadata_path):
            try:
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    existing_results = json.load(f)
            except (json.JSONDecodeError, IOError):
                existing_results = []
        
        # Merge results, deduplicate by entry_id
        result_dict = {item['entry_id']: item for item in existing_results}
        for result in results:
            result_dict[result['entry_id']] = result
        
        # Remove records older than 30 days
        current_date = datetime.now()
        filtered_results = []
        for result in result_dict.values():
            try:
                published_date = datetime.fromisoformat(result['published'])
                if (current_date - published_date).days < 30:
                    filtered_results.append(result)
            except (ValueError, KeyError):
                # Keep records with invalid or missing dates
                filtered_results.append(result)
        
        # Sort by published date (descending)
        filtered_results.sort(key=lambda x: x.get('published', ''), reverse=True)
        
        # Save results
        try:
            os.makedirs(output_dir, exist_ok=True)
            with open(metadata_path, 'w', encoding='utf-8') as f:
                json.dump(filtered_results, f, indent=2, ensure_ascii=False)
            print(f"Metadata saved to: {metadata_path} (total {len(filtered_results)} records)")
        except IOError as e:
            print(f"Error saving metadata: {e}")

    def _create_search_query(self, query: str = "",
                           categories: Optional[List[str]] = None,
                           keywords: Optional[Dict[str, List[str]]] = None) -> str:
        """Build an advanced search query."""
        search_parts = []
        
        # Add base query
        if query:
            if self.config['title_only']:
                search_parts.append(f"ti:{query}")
            elif self.config['abstract_only']:
                search_parts.append(f"abs:{query}")
            elif self.config['author_only']:
                search_parts.append(f"au:{query}")
            else:
                search_parts.append(query)

        # Add categories (OR across categories)
        if categories:
            try:
                cat_parts = []
                for cat in categories:
                    if not cat:
                        continue
                    if self.config['include_cross_listed']:
                        cat_parts.append(f"cat:{cat}")
                    else:
                        cat_parts.append(f"primary_cat:{cat}")
                
                if cat_parts:
                    cat_query = " OR ".join(cat_parts)
                    search_parts.append(f"({cat_query})")
            except Exception as e:
                print(f"Debug - error building category query: {e}")

        final_query = " AND ".join(search_parts) if search_parts else "*:*"
        return final_query

    def search_papers(self, 
                     categories: Optional[List[str]] = None,
                     query: str = QUERY,
                     last_run_file: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Search papers and return metadata. Supports multiple categories and deduping.
        
        Args:
            categories: arXiv category list
            query: Search keywords
            last_run_file: File path to store last run info (optional)
        """
        all_results = []
        
        # Load latest entry ID from last run
        last_entry_id = None
        if last_run_file and os.path.exists(last_run_file):
            last_entry_id = self._load_last_run_info(last_run_file)
            if last_entry_id:
                print(f"Found last run record. Will start from entry ID: {last_entry_id}")
            else:
                print("Found last run file but entry_id is invalid.")
        
        # Build query
        search_query = self._create_search_query(query, categories)
        print(f"Using query: {search_query}")
        
        # Set sort options
        sort_criterion = getattr(arxiv.SortCriterion, self.config['sort_by'])
        sort_order = getattr(arxiv.SortOrder, self.config['sort_order'])
        
        try:
            # Build search args
            search_kwargs = {
                'query': search_query,
                'max_results': self.config['max_total_results'],
                'sort_by': sort_criterion,
                'sort_order': sort_order
            }
            
            # Only add id_list if not None
            if self.config['id_list'] is not None:
                search_kwargs['id_list'] = self.config['id_list']
            
            search = arxiv.Search(**search_kwargs)
            latest_entry_id = None

            for paper in self.client.results(search):
                try:
                    # Record the first entry ID
                    if not latest_entry_id:
                        latest_entry_id = paper.entry_id

                    # Stop when we reach the last processed entry
                    if last_entry_id and paper.entry_id == last_entry_id:
                        print(f"Reached last processed entry (ID: {last_entry_id}); stopping.")
                        break

                    metadata = {
                        'title': paper.title,
                        'authors': [author.name for author in paper.authors],
                        'published': paper.published.isoformat(),
                        'updated': paper.updated.isoformat(),
                        'summary': paper.summary,
                        'doi': paper.doi,
                        'primary_category': paper.primary_category,
                        'categories': self._safe_get_categories(paper),
                        'links': [link.href for link in paper.links],
                        'pdf_url': paper.pdf_url,
                        'entry_id': paper.entry_id,
                        'comment': getattr(paper, 'comment', '')
                    }
                    all_results.append(metadata)
                    
                except Exception as e:
                    print(f"Error processing paper: {e}")
                    continue
                
        except Exception as e:
            print(f"Search error: {e}")
            print(f"Error type: {type(e)}")
            import traceback
            print(f"Traceback: {traceback.format_exc()}")

        if not all_results:
            print("No new papers found.")
        else:
            print(f"Found {len(all_results)} new papers.")

        return all_results
