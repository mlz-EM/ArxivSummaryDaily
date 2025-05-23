import os
import argparse
from datetime import datetime
from .job_summarizer import JobSummarizer
from config.settings import JOB_CONFIG, LLM_CONFIG, OUTPUT_DIR
from jobspy import scrape_jobs

def main():
    parser = argparse.ArgumentParser(description='工作概况生成工具')
    parser.add_argument('--output-dir', type=str, default=OUTPUT_DIR, help='输出目录')
    
    args = parser.parse_args()
    
    
    # 初始化客户端
    job_summarizer = JobSummarizer(LLM_CONFIG['api_key'], LLM_CONFIG.get('model'))
        
    # 获取工作
    jobs = scrape_jobs(**JOB_CONFIG)
    jobs = jobs.sort_values(by='date_posted', ascending=False)
    jobs = jobs.to_dict(orient="records")
    if not jobs:
        print("未找到符合条件的工作")
        return
    
    # 生成摘要
    output_file = os.path.join(args.output_dir, f"jobsDaily.md")
    
    # 创建输出目录
    os.makedirs(args.output_dir, exist_ok=True)
    
    # 生成摘要并保存
    try:
        success = job_summarizer.summarize_jobs(jobs, output_file)
        if success:
            print(f"摘要已成功生成并保存到: {output_file}")
        else:
            print(f"摘要生成过程中出现错误，结果可能不完整: {output_file}")
    except Exception as e:
        print(f"生成摘要时发生错误: {e}")
        success = False
    
    # 只有在摘要成功生成后才保存最新文章ID
    if success:
        print(f"摘要成功生成。")
    else:
        print("摘要生成不完整或失败。")

if __name__ == '__main__':
    main()