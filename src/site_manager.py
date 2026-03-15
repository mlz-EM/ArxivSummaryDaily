#!/usr/bin/env python
# -*- coding: utf-8 -*-

import shutil
import argparse
from datetime import datetime, timedelta
import re
from pathlib import Path
import os
import subprocess

class SiteManager:
    """ArXiv summary site manager: cleanup, index, and archive generation."""
    
    # Default front matter template
    DEFAULT_FRONT_MATTER = """---
layout: default
title: {title}
---

"""
    
    def __init__(self, data_dir, github_dir=None):
        """Initialize site manager.

        Args:
            data_dir: Data directory path
            github_dir: GitHub config directory path
        """
        self.data_dir = Path(data_dir)
        self.github_dir = Path(github_dir) if github_dir else None
        self.data_dir.mkdir(exist_ok=True)  # Ensure data directory exists
    
    def _escape_markdown_chars(self, text):
        """Escapes '|' and '_' characters in markdown text unless already escaped."""
        # Escape '|' that is not already escaped
        text = re.sub(r'(?<!\\)\|', r'\\|', text) # Use r'(?<!\\)\|' to match '|' not preceded by '\'
        # Escape '_' that is not already escaped
        text = re.sub(r'(?<!\\)\_', r'\\_', text) # Use r'(?<!\\)_' to match '_' not preceded by '\'
        return text
    
    def clean_old_files(self, days=30):
        """Remove markdown files older than the given number of days.

        Args:
            days: Max age (days) to keep files

        Returns:
            Number of deleted files
        """
        print(f"Removing markdown files older than {days} days...")
        
        current_time = datetime.now()
        max_age = timedelta(days=days)
        
        # Find all summary files
        summary_files = list(self.data_dir.glob("summary_*.md"))
        removed_count = 0
        
        for file_path in summary_files:
            # Prefer timestamp in filename to avoid checkout time issues
            file_datetime = self._get_summary_datetime(file_path)
            age = current_time - file_datetime

            # Delete files older than the threshold
            if age >= max_age:
                file_date = file_datetime.strftime('%Y-%m-%d %H:%M:%S')
                print(f"Deleting old file: {file_path} ({file_date})")
                file_path.unlink()
                removed_count += 1
        
        print(f"Cleanup complete. Deleted {removed_count} files.")
        return removed_count

    def _get_summary_datetime(self, file_path):
        """Parse timestamp from filename; fallback to file mtime."""
        match = re.search(r'summary_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})', file_path.name)
        if match:
            year, month, day, hour, minute, second = map(int, match.groups())
            return datetime(year, month, day, hour, minute, second)
        return datetime.fromtimestamp(file_path.stat().st_mtime)
    
    def get_sorted_summary_files(self):
        """Get summary files sorted by time (newest first).

        Returns:
            Sorted list of file paths
        """
        summary_files = list(self.data_dir.glob("summary_*.md"))
        
        # Sort by mtime (newest first)
        if summary_files:
            summary_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        
        return summary_files
    
    def extract_content(self, file_path):
        """Extract content and remove front matter if present.

        Args:
            file_path: File path

        Returns:
            (title, content) tuple
        """
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Remove front matter if present
        if content.startswith('---'):
            parts = content.split('---', 2)
            if len(parts) >= 3:
                content = parts[2].strip()
        
        # Extract title
        title_match = re.search(r'^# (.*?)$', content, re.MULTILINE)
        title = title_match.group(1) if title_match else "ArXiv Summary Daily"
        
        return title, content
    
    def copy_latest_to_index(self, sorted_files=None):
        """Copy latest md file to index.md.

        Args:
            sorted_files: Optional pre-sorted file list

        Returns:
            True on success
        """
        if sorted_files is None:
            sorted_files = self.get_sorted_summary_files()
        
        index_path = self.data_dir / "index.md"
        today = datetime.now().strftime('%Y-%m-%d')
        
        if sorted_files:
            latest_file = sorted_files[0]
            with open(latest_file, 'r', encoding='utf-8') as f:
                content = f.read()
        
            # Escape markdown characters
            escaped_content = self._escape_markdown_chars(content)
            
            # Write back file
            with open(latest_file, 'w', encoding='utf-8') as f:
                f.write(escaped_content)
            
            print(f"Latest file found: {latest_file}")
            print("Updating index.md...")
            
            # Extract content and title
            title, content = self.extract_content(latest_file)
            
            # Add archive link
            archive_link = f"[View all archives](archive.md) | Updated: {today}\n\n"
            
            # Build full content
            full_content = self.DEFAULT_FRONT_MATTER.format(title=title) + archive_link + content
            
            # Write file
            with open(index_path, 'w', encoding='utf-8') as f:
                f.write(full_content)
                
            print("index.md updated successfully.")
        else:
            # If no files found, create a simple index.md
            print("No summary files found. Creating an empty index.md.")
            default_content = "[View all archives](archive.md)\n\n# ArXiv Summary Daily\n\nNo summaries available yet.\n"
            full_content = self.DEFAULT_FRONT_MATTER.format(title="ArXiv Summary Daily") + default_content
            
            with open(index_path, 'w', encoding='utf-8') as f:
                f.write(full_content)
        
        return True
    
    def create_archive_page(self, sorted_files=None):
        """Create archive page listing all summaries.

        Args:
            sorted_files: Optional pre-sorted file list

        Returns:
            True on success
        """
        if sorted_files is None:
            sorted_files = self.get_sorted_summary_files()
        
        archive_path = self.data_dir / "archive.md"
        print(f"Creating archive page: {archive_path}")
        
        # Prepare content
        header = "[Back to home](index.md)\n\n# ArXiv Summary Archive\n\nAll available ArXiv summaries, sorted by date (newest first):\n\n"
        content = self.DEFAULT_FRONT_MATTER.format(title="ArXiv Summary Archive") + header
        
        # Process each file and ensure it has front matter
        for file_path in sorted_files:
            filename = file_path.name
            # Extract date from filename (format: summary_YYYYMMDD_HHMMSS.md)
            match = re.search(r'summary_(\d{4})(\d{2})(\d{2})_', filename)
            if match:
                year, month, day = match.groups()
                formatted_date = f"{year}-{month}-{day}"
                
                # Ensure summary file has front matter
                self.ensure_file_has_front_matter(file_path, f"{formatted_date} ArXiv Summary")
                
                # Add link to archive page
                content += f'- [{formatted_date} Summary]({filename})\n'
        
        # Write file
        with open(archive_path, 'w', encoding='utf-8') as f:
            f.write(content)
            
        print("Archive page created successfully.")
        return True
    
    def ensure_file_has_front_matter(self, file_path, title):
        """Ensure Jekyll front matter exists; add if missing.

        Args:
            file_path: File path
            title: File title
        """
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # If front matter already exists, do nothing
        if content.startswith('---'):
            return
        
        # Add front matter
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(self.DEFAULT_FRONT_MATTER.format(title=title) + content)
    
    def setup_site_structure(self):
        """Set up Jekyll deployment to serve index.md directly (no nav).

        Returns:
            True on success
        """
        if not self.github_dir:
            print("GitHub config directory not provided. Skipping site setup.")
            return False
            
        # 1. Copy config file
        config_src = self.github_dir / "_config.yml"
        config_dest = self.data_dir / "_config.yml"
        
        if config_src.exists():
            shutil.copy2(config_src, config_dest)
        
        # 2. Create a simple Gemfile for GitHub Pages
        gemfile_path = self.data_dir / "Gemfile"
        gemfile_content = 'source "https://rubygems.org"\ngem "github-pages", group: :jekyll_plugins\ngem "jekyll-theme-cayman"\n'
        with open(gemfile_path, 'w', encoding='utf-8') as f:
            f.write(gemfile_content)
        
        # 3. Ensure index.md has front matter
        index_path = self.data_dir / "index.md"
        if index_path.exists():
            with open(index_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Add front matter if missing
            if not content.startswith('---'):
                title, main_content = self.extract_content(index_path)
                with open(index_path, 'w', encoding='utf-8') as f:
                    f.write(self.DEFAULT_FRONT_MATTER.format(title=title) + main_content)
        
        # 4. Copy layout config
        layouts_dir = self.data_dir / "_layouts"
        mathjax_src = self.github_dir / "_layouts" / "default.html"
        if mathjax_src.exists():
            layouts_dir.mkdir(exist_ok=True)
            mathjax_dest = layouts_dir / "default.html"
            shutil.copy2(mathjax_src, mathjax_dest)
        
        # 5. Copy mathjax.html
        includes_dir = self.data_dir / "_includes"
        includes_src = self.github_dir / "_includes" / "mathjax.html"
        if includes_src.exists():
            includes_dir.mkdir(exist_ok=True)
            includes_dest = includes_dir / "mathjax.html"
            shutil.copy2(includes_src, includes_dest)
        
        # 6. Copy logo image
        img_dir = self.data_dir / "img"
        img_dir.mkdir(exist_ok=True)
        
        logo_src = self.github_dir / "img" / "paper.png"
        if logo_src.exists():
            logo_dest = img_dir / "paper.png"
            print(f"Copying site logo: {logo_src} -> {logo_dest}")
            shutil.copy2(logo_src, logo_dest)
        else:
            print(f"Warning: logo file not found {logo_src}")
        
        # 7. Remove .nojekyll if present (we want Jekyll)
        nojekyll_path = self.data_dir / ".nojekyll"
        if nojekyll_path.exists():
            nojekyll_path.unlink()
        
        print("Jekyll setup complete - deploying index.md directly.")
        return True

def main():
    """Main entry: parse CLI args and run site tasks."""
    parser = argparse.ArgumentParser(description="ArXiv Summary site manager")
    parser.add_argument('--data-dir', default='./data', help='Data directory (default: ./data)')
    parser.add_argument('--github-dir', default='./.github', help='GitHub config directory (default: ./.github)')
    parser.add_argument('--days', type=int, default=30, help='Days of summaries to keep (default: 30)')
    parser.add_argument('--skip-clean', action='store_true', help='Skip cleanup of old files')
    args = parser.parse_args()
    
    # Create site manager
    site = SiteManager(args.data_dir, args.github_dir)
    
    # Clean old files
    if not args.skip_clean:
        site.clean_old_files(args.days)
    
    # Get sorted files (once)
    sorted_files = site.get_sorted_summary_files()
    
    # Run tasks
    site.copy_latest_to_index(sorted_files)
    site.create_archive_page(sorted_files)
    site.setup_site_structure()
    
    print("All tasks completed.")

if __name__ == "__main__":
    main()
