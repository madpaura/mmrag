import urllib3
from bs4 import BeautifulSoup, Tag
import requests
import os
import os
import pandas as pd
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin
from urllib.parse import unquote
from threading import Lock
import queue as Queue

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_10_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/39.0.2171.95 Safari/537.36'}

class ConfluenceClient:
    def __init__(self,token):
        self.base_url = "https://confluence.samsungds.net"
        self.token = token
        self.headers = {
                            "Authorization": f"Bearer {self.token}",
                            "Content-Type": "application/json"
                }
        self.visited = set()
        self.visited_lock = Lock()
        self.jira_tickets_lock = Lock()
        self.executor = None
    
    def write_to_file(self,directory,file,content):
        os.makedirs(directory,exist_ok=True)
        filepath=os.path.join(directory,file)
        with open(filepath, 'w', encoding="utf-8-sig") as f:
            f.write("\n".join(content))
        f.close()
        print("File " + filepath + " Saved Successfully !!")

    def html_table_to_markdown(self,table):
        rows = []
        for tr in table.find_all("tr"):
            row = []
            for cell in tr.find_all(["th", "td"]):
                text = cell.get_text("", strip=True)
                row.append(text)
            if row:
                rows.append(row)
        if not rows:
            return ""
        # Normalize row lengths
        max_cols = max(len(r) for r in rows)
        rows = [r + [""] * (max_cols - len(r)) for r in rows]
        df = pd.DataFrame(rows[1:], columns=rows[0])
        return df.to_markdown(index=False)
    
    def get_page_content_from_link(self,page_path):
        page_id = self.extract_confluence_page_id(page_path)

        try:
        
            response = requests.get(f"{self.base_url}/plugins/viewstorage/viewpagestorage.action?pageId={page_id}", stream=True, headers=self.headers, verify=False)

            response.raise_for_status()
        except requests.exceptions.HTTPError as http_err:   
                print(f"Your are Not authorized to access the page {page_path}")  
                return
        except Exception as err:   
                print(f"Other error occurred: {err}")
                return
                
        soup = BeautifulSoup(response.text, "html.parser")    

        jira_pattern = re.compile(  
                    r'(https?://(mlm\.jira\.samsungds\.net|sj-jira\.ssi\.samsung\.com:8080|ssirjirasw\.samsungds\.net)/browse/[\w-]+)'  
                )
        
        # 2. Find all 'a' tags where the 'href' matches the pattern  
        jiralinks = []  
        for a in soup.find_all('a', href=jira_pattern):  
            link = a['href']  
            jiralinks.append(link)
      
        root = (  
            soup.find("article")  
            or soup.find("main")  
            or soup.body  
            or soup  
        ) 

        markdown_parts = []  
        
        # We target the top-level structural elements  
        for elem in root.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'ul', 'ol', 'table']):  
            # Crucial Fix: Only process the element if its parent is the root   
            # or if it's not inside a table (since table is handled as a whole).  
            if elem.find_parent("table"):  
                continue

            if elem.name in ["h1", "h2", "h3", "h4", "h5", "h6"]:  
                level = int(elem.name[1])  
                text = elem.get_text(" ", strip=True)  
                if text:  
                    markdown_parts.append(f"\n{'#' * level} {text}\n")

            elif elem.name == "p":  
                text = elem.get_text(" ", strip=True)  
                if text:  
                    markdown_parts.append(f"\n{text}\n")

            elif elem.name == "ul":  
                for li in elem.find_all("li", recursive=False):  
                    text = li.get_text(" ", strip=True)  
                    markdown_parts.append(f"- {text}")  
                markdown_parts.append("")

            elif elem.name == "ol":  
                for idx, li in enumerate(elem.find_all("li", recursive=False), 1):  
                    text = li.get_text(" ", strip=True)  
                    markdown_parts.append(f"{idx}. {text}")  
                markdown_parts.append("")

            elif elem.name == "table":  
                md_table = self.html_table_to_markdown(elem)  
                if md_table:  
                    markdown_parts.append("\n")  
                    markdown_parts.append(md_table)  
                    markdown_parts.append("\n")

        clean_md, diagram_names = self.process_diagram_patterns(markdown_parts)

        diagrams_mx_details = []

        diagrams_mx_details = self.fetch_diagram_details(diagram_names,page_id)

        return self.write_images_in_md_file(clean_md,diagrams_mx_details,diagram_names),jiralinks

        #return clean_md,jiralinks

    def process_diagram_patterns(self, md_content_list):       
        pattern = r"^\s*true\s+(.+?)\s+false\s+.*$"    
        replacement = r"diagram:\1"    
        
        processed_list = []    
        diagram_names = []  # List to store the extracted names  
        
        for line in md_content_list:    
            # 1. Check if the pattern exists to extract the diagram name  
            match = re.search(pattern, line)  
            if match:  
                # match.group(1) refers to the (.+?) part of the regex  
                diagram_names.append(match.group(1))  
            
            # 2. Replace the pattern as before  
            new_line = re.sub(pattern, replacement, line)    
            processed_list.append(new_line)    
        
        # Return both the modified content and the list of names  
        return processed_list, diagram_names 

    def fetch_diagram_details(self,diagram_names,page_id):
        mx_contents = []
        for diagram in diagram_names:
            response = requests.get(f"{self.base_url}/download/attachments/{page_id}/{diagram}?api=v2", stream=True, headers=self.headers, verify=False)
            mx_contents.append(response.text)

        return mx_contents



    def write_images_in_md_file(self,clean_md,diagrams_mx_details,diagram_names):
        updated_md = list(clean_md)
        for i, name in enumerate(diagram_names):  
            placeholder = f"diagram:{name}"  
            replacement = diagrams_mx_details[i]  
            for line_idx in range(len(updated_md)):  
                if placeholder in updated_md[line_idx]:  
                    updated_md[line_idx] = updated_md[line_idx].replace(placeholder, replacement)
        return updated_md

    def get_page_info(self,page_link):
        page_id = self.extract_confluence_page_id(page_link)

        try:
            response = requests.get(f"{self.base_url}/pages/viewinfo.action?pageId={page_id}",headers=self.headers, verify=False)

            response.raise_for_status()
        except requests.exceptions.HTTPError as http_err:   
                print(f"Your are Not authorized to access this page {self.base_url}/pages/viewinfo.action?pageId={page_id}")  
                return
        except Exception as err:   
                print(f"Other error occurred: {err}")
                return

        soup = BeautifulSoup(response.text, 'html.parser')  
        
        start_node = soup.find(string=re.compile(r'Children\s*\(\d+\)', re.IGNORECASE))  
        
        if not start_node:  
            #print("Could not find the 'Children' section.")  
            return []

        current_element = start_node.find_parent()  
        links_list = []
 
        for element in current_element.find_all_next():  
            if element.name == 'string' or hasattr(element, 'text'):  
                if "Labels" in element.get_text():  
                    break  
            
            if element.name == 'a' and element.has_attr('href'):  
                link_text = element.get_text(strip=True)  
                href = element['href']  
                full_url = urljoin(page_link, href)  
                
                if "Page:" in link_text or link_text != "":  
                    links_list.append(full_url)

        return links_list    

    def get_filename_from_pagetitle(self,url):    
        title = url.rstrip('/').split('/')[-1]  
        
        decoded_title = unquote(title)  
         
        cleaned_title = decoded_title.replace('+', '_')  
         
        cleaned_title = re.sub(r'_+', '_', cleaned_title)  
         
        cleaned_title = cleaned_title.strip('_')  
        
        return cleaned_title

    def _process_single_page(self, page_link, write_to_file, directory, prefix, jira_tickets):
        """Process a single page - designed to be called by thread pool"""
        filename_base = self.get_filename_from_pagetitle(page_link)
        full_filename = f"{prefix}_{filename_base}" if prefix else f"1_{filename_base}"

        with self.visited_lock:
            if page_link in self.visited:
                return None
            self.visited.add(page_link)

        try:
            content, jiratickets = self.get_page_content_from_link(page_link)
            
            with self.jira_tickets_lock:
                jira_tickets.append(jiratickets)
                
            if write_to_file:
                self.write_to_file(directory, full_filename + ".md", content)
            
            return page_link
        except Exception as e:
            print(f"Error processing page {page_link}: {e}")
            return None

    def get_page_content(self, page_link, write_to_file=False, directory=None, depth=1, prefix="", jira_tickets=None):
        if jira_tickets is None:
            jira_tickets = []
        
        from collections import deque
        
        
        work_queue = Queue.Queue()
        pending_count = {'value': 0}
        pending_lock = Lock()
        
        def submit_work(link, current_depth, current_prefix):
            with pending_lock:
                pending_count['value'] += 1
            work_queue.put((link, current_depth, current_prefix))
        
        def worker():
            while True:
                try:
                    link, current_depth, current_prefix = work_queue.get(timeout=5)
                    
                    if current_depth <= 0:
                        with pending_lock:
                            pending_count['value'] -= 1
                        work_queue.task_done()
                        continue
                    
                    
                    processed_link = self._process_single_page(
                        link, write_to_file, directory, current_prefix, jira_tickets
                    )
                    
                    if processed_link and current_depth > 1:
                        child_pages = self.get_page_info(link)
                        for i, child_link in enumerate(child_pages, start=1):
                            child_prefix = f"{current_prefix}.{i}" if current_prefix else f"1.{i}"
                            submit_work(child_link, current_depth - 1, child_prefix)
                    
                    with pending_lock:
                        pending_count['value'] -= 1
                    
                    work_queue.task_done()
                    
                except Queue.Empty:
                    with pending_lock:
                        if pending_count['value'] == 0:
                            break
                except Exception as e:
                    print(f"Error in worker processing: {e}")
                    with pending_lock:
                        pending_count['value'] -= 1
                    work_queue.task_done()
        
        submit_work(page_link, depth, prefix)
        
        with ThreadPoolExecutor(max_workers=16) as executor:

            worker_futures = [executor.submit(worker) for _ in range(16)]
            work_queue.join()
            
        return jira_tickets

    def extract_confluence_page_id(self,link):   
        pattern = r'/pages/(\d+)'  
        
        match = re.search(pattern, link)  
        
        if match:  
            return match.group(1)  
        else:  
            return None