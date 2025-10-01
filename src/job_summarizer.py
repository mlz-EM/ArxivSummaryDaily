"""
工作总结模块 - 使用大语言模型API生成工作机会概况
"""
import os
import json
from typing import List, Dict, Any, Optional
from pathlib import Path
import requests
import time
from datetime import datetime
import pytz
from config.settings import LLM_CONFIG, QUERY

class ModelClient:
    """语言模型API客户端"""
    
    def __init__(self, api_key: str, model: Optional[str] = None):
        self.api_key = api_key
        self.model = model or LLM_CONFIG['model']
        self.api_url = f"{LLM_CONFIG['api_url']}/{self.model}:generateContent"
        self.timeout = LLM_CONFIG.get('timeout', 30)
        
    def _create_headers(self) -> Dict[str, str]:
        """创建请求头"""
        return {
            "Content-Type": "application/json"
        }
    
    def _create_request_body(
        self, 
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None
    ) -> Dict[str, Any]:
        """创建请求体"""
        # 将最后一条消息作为提示词
        prompt = messages[-1]["content"]
        
        return {
            "contents": [{
                "parts": [{
                    "text": prompt
                }]
            }],
            "generationConfig": {
                "temperature": temperature or LLM_CONFIG['temperature'],
                "maxOutputTokens": max_tokens or LLM_CONFIG['max_output_tokens'],
                "topP": LLM_CONFIG['top_p'],
                "topK": LLM_CONFIG['top_k']
            }
        }
    
    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None
    ) -> Dict[str, Any]:
        """创建聊天完成"""
        headers = self._create_headers()
        data = self._create_request_body(messages, temperature, max_tokens)
        
        for attempt in range(LLM_CONFIG['retry_count']):
            try:
                response = requests.post(
                    f"{self.api_url}?key={self.api_key}",
                    headers=headers,
                    json=data,
                    timeout=self.timeout
                )
                
                if response.status_code != 200:
                    raise Exception(f"API 调用失败: {response.text}")
                    
                result = response.json()
                
                return {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": result["candidates"][0]["content"]["parts"][0]["text"]
                        },
                        "finish_reason": "stop"
                    }],
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0
                    }
                }
            except requests.Timeout:
                print(f"请求超时（{self.timeout}秒），正在重试...")
                if attempt == LLM_CONFIG['retry_count'] - 1:
                    raise TimeoutError(f"API调用在{self.timeout}秒内未响应，已重试{LLM_CONFIG['retry_count']}次")
                time.sleep(LLM_CONFIG['retry_delay'] * (2 ** attempt))
            except Exception as e:
                if attempt == LLM_CONFIG['retry_count'] - 1:
                    raise
                time.sleep(LLM_CONFIG['retry_delay'] * (2 ** attempt))

class JobSummarizer:
    def __init__(self, api_key: str, model: Optional[str] = None):
        self.client = ModelClient(api_key, model)
        self.max_papers_per_batch = 20

    def _generate_batch_summaries(self, jobs: List[Dict[str, Any]], start_index: int) -> str:
        """为一批论文生成总结"""
        batch_prompt = ""
        for i, job in enumerate(jobs, start=start_index):
            batch_prompt += f"""
job {i}：
title: {job['title']}
school: {job['company']}
location: {job['location']}
posted: {job['date_posted']}
description: {job['description']}
job_url: {job['job_url']}
"""
        
        final_prompt = f"""我是一名材料工程系的博士毕业生，我的研究领域是用电子显微镜在微观尺度上进行材料表征并建立其结构与性能之间的联系。目前我在寻找北美tenure tracked的教职。我将提供{len(jobs)}个潜在工作机会，请根据description或者job_url的内容分别生成markdown语言格式的总结。对每份工作：
1. 删除领域完全不相关的工作，例如文科类工作，医学院工作，以及管理类工作。
2. 删除不是tenure tracked的工作，例如teaching faculty或者adjunct professor。
3. 查询学校是否为R1，如果不是请删除该工作。
4. 如果全部工作都不满足以上条件，也请保留一份最相关的工作并生成总结。
5. 根据与我背景的对工作相关程度对筛选后的工作进行打分 从一颗到三颗🌟
6. 在工作描述中提取一句话的关键词进行总结，最好是工作需要的具体方向或者department
请用英文回答，保持原有格式，对每份工作的回答后加入markdown格式的"---"分隔符。
确保每份工作信息与提供的内容保持一致。
你的输出环境同时支持markdown和LaTeX语法渲染
输出格式为：

**[title](job_url)** 🌟🌟
- **Location**: school at location
- **Date**: YYYY-MM-DD
- **Description**: summary
---
**[title](job_url)** 🌟
- **Location**: school at location
- **Date**: YYYY-MM-DD
- **Description**: summary
---
......
---
**[title](job_url)** 🌟🌟🌟
- **Location**: school at location
- **Date**: YYYY-MM-DD
- **Description**: summary
---

请注意，以上是对每份工作的总结格式示例。请确保输出格式与示例一致。不要添加任何额外信息，只生成规定格式的总结内容即可。请一定确保格式争取。

以下是一个示例：

---
**[Assistant Professor in Materials Sciecne Department](http://linkedin.com/job)** 🌟🌟🌟
- **Location**: Harvard Univeersity at Boston, USA
- **Date**: 2025-01-11
- **Description**: Department of Materials Sciecne is looking for TT prof to work on the characterization of energy-related materials.
---

请根据以下工作信息生成总结：
{batch_prompt}"""

        try:
            response = self.client.chat_completion([{
                "role": "user",
                "content": final_prompt
            }])
            nr = response["choices"][0]["message"]["content"].strip().count('**Description**:')
            print(f"批量处理完成，生成{nr}份总结...")
            return response["choices"][0]["message"]["content"].strip()
        except Exception as e:
            # 如果批处理失败，生成错误信息
            error_summaries = []
            for i, job in enumerate(jobs, start=start_index):
                error_summaries.append(f"""
job {i}：
title: {job['title']}
school: {job['company']}
location: {job['location']}
posted: {job['date_posted']}
description: {job['description']}
job_url: {job['job_url']}
summary: [生成失败: {str(e)}]
---""")
            return "\n".join(error_summaries)

    def _process_batch(self, jobs: List[Dict[str, Any]], start_index: int) -> str:
        """处理一批工作"""
        print(f"正在批量处理 {len(jobs)} 份工作...")
        summaries = self._generate_batch_summaries(jobs, start_index)
        time.sleep(2)  # 在批次之间添加短暂延迟
        return summaries

    def _generate_batch_summary(self, jobs: List[Dict[str, Any]]) -> str:
        """批量生成所有工作的总结"""
        all_summaries = []
        total_jobs = len(jobs)
        
        for i in range(0, total_jobs, self.max_papers_per_batch):
            batch = jobs[i:i + self.max_papers_per_batch]
            print(f"\n正在处理第 {i + 1} 到 {min(i + self.max_papers_per_batch, total_jobs)} 份工作...")
            batch_summary = self._process_batch(batch, i + 1)
            all_summaries.append(batch_summary)
            
            if i + self.max_papers_per_batch < total_jobs:
                print("批次处理完成，等待3秒后继续...")
                time.sleep(3)  # 批次之间的冷却时间
        
        return "\n".join(all_summaries)

    def summarize_jobs(self, jobs: List[Dict[str, Any]], output_file: str) -> bool:
        """
        批量处理所有论文并创建Markdown报告
        
        Args:
            papers: 论文列表
            output_file: 输出文件路径
            
        Returns:
            bool: 摘要生成是否真正成功。如果生成的摘要包含错误信息则返回False
        """
        api_success = True  # 标记API调用是否成功
        
        try:
            # 生成总结内容
            print(f"开始生成工作总结，共 {len(jobs)} 份...")
            summaries = self._generate_batch_summary(jobs)
            
            # 检查生成的摘要是否包含错误信息
            if "[生成失败:" in summaries:
                api_success = False
                print("警告: 摘要生成过程中出现错误，结果可能不完整")
            
            # 转换为markdown格式
            markdown_content = self._generate_markdown(jobs, summaries)
            
            # 保存为markdown文件
            output_md = output_file.replace('.pdf', '.md')
            with open(output_md, 'w', encoding='utf-8') as f:
                f.write(markdown_content)
            print(f"Markdown文件已保存：{output_md}")
            
            return api_success
            
        except Exception as e:
            # 如果生成总结失败，保存基本信息为markdown格式
            beijing_time = datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d %H:%M:%S')
            error_content = f"""# Arxiv论文总结报告

生成时间：{beijing_time}

**生成总结时发生错误，以下是论文基本信息：**

"""
            for i, job in enumerate(jobs, 1):
                error_content += f"""
job {i}：
title: {job['title']}
school: {job['company']}
location: {job['location']}
posted: {job['date_posted']}
description: {job['description']}
job_url: {job['job_url']}
summary: [生成失败: {str(e)}]
"""
            
            # 保存错误信息为markdown文件
            error_md = output_file.replace('.pdf', '_error.md')
            with open(error_md, 'w', encoding='utf-8') as f:
                f.write(error_content)
            print(f"发生错误，已保存基本信息到：{error_md}")
            
            return False  # 发生异常，摘要生成肯定失败

    def _generate_markdown(self, jobs: List[Dict[str, Any]], summaries: str) -> str:
        """生成markdown格式的报告"""
        beijing_time = datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d %H:%M:%S')
        
        markdown_content = f"""
# Basic Info
- This report was automatically generated by **{self.client.model}** at **{beijing_time}**.  
- It includes the recent tenure tracked position in engineering related field from Google, LinkedIn and Indeed.  
- This page is powered by [ArxivSummaryDaily](https://github.com/dong-zehao/ArxivSummaryDaily) and [JobSpy](https://github.com/speedyapply/JobSpy).
---
{summaries}
"""
        return markdown_content
