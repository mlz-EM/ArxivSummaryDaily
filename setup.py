from setuptools import setup, find_packages

setup(
    name="arxivsummary",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "arxiv",
        "python-dotenv",
        "python-jobspy"
    ],
    entry_points={
        'console_scripts': [
            'arxivsummary=src.cli:main',
            'jobsummary=src.jobcli:main',
            'arxivsite=src.site_manager:main',  # 添加新的命令行入口点
        ],
    },
    author="DongZehao",
    description="A tool for generating daily summaries of arXiv papers",
    python_requires=">=3.9",
)