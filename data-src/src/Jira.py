import urllib3
import os
import json
import requests
import re
from parser import Parser
from bs4 import BeautifulSoup, Tag
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from parser import Parser


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_10_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/39.0.2171.95 Safari/537.36'}


class JiraClient:
    def __init__(self,base_url,token):
        self.base_url = base_url
        self.token = token
        self.search_endpoint = f"{self.base_url}rest/api/2/search"
        self.headers = {  
                    "Authorization": f"Bearer {self.token}",  
                    "Content-Type": "application/json"  
                }

    def process_jira_ticket(self,output_dir,ticket):

        jql_query = f'key = "{ticket.strip()}"'
                    
        params = {  
                    "jql": jql_query,  
                    # "maxResults": 2, 
                    "fields": "assignee,reporter,comment,issuetype,summary,description,watches,updated,priority,status,creator,created" 
                    # "fields": "*all"
                }
        try:
            response = requests.get(  
                        self.search_endpoint,  
                        params=params,  
                        headers=self.headers, 
                        verify=False, 
                    )
            
            response.raise_for_status()
        except requests.exceptions.HTTPError as http_err:   
            print(f"Your are Not authorized to access the ticket {ticket}")  
            return
        except Exception as err:   
            print(f"Other error occurred: {err}")
            return

        data = response.json()

        parser = Parser()

        parser.parse_jira_json(output_dir,data["issues"])

    def generate_md_files_from_jira_tickets(self,jira_tickets,output_dir):

        contents = []
        
        with ThreadPoolExecutor(max_workers=16) as executor:
    
                futures = [
                    executor.submit(
                        self.process_jira_ticket,
                        output_dir,
                        ticket
                    )
                    for ticket in jira_tickets
                ]
    
        for future in as_completed(futures):
            result = future.result()
    
            if result:
                contents.append(result)

        

            

            

            


            
        
