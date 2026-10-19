from Jira import JiraClient
from confluence import ConfluenceClient
import os  
import re  
from dotenv import load_dotenv
from parser import Parser
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
import glob
from urllib.parse import urlparse

load_dotenv("config/.env")

def generate_jira_md_file(output_dir,jira_ticket_list,base_url,token):

    jiraclient = JiraClient(base_url,token)
    jiraclient.generate_md_files_from_jira_tickets(jira_ticket_list,output_dir)

def read_root_confluence_link_from_env():
    links = []
    try:  
        with open('config/.env', 'r', encoding='utf-8') as f:  
            lines = f.readlines()  
            recording = False  
            for line in lines:  
                stripped_line = line.strip()  
                if not stripped_line or stripped_line.startswith('#'):  
                    continue  
                if stripped_line.startswith("CONFLUENCE_ROOT_LINK="):  
                    recording = True  
                    value = stripped_line.split("=", 1)[1].strip()  
                    if value:  
                        links.append(value)  
                    continue
                if recording: 
                    # if "=" in stripped_line:  
                    #     break 
                    links.append(stripped_line)  
    except FileNotFoundError:  
        print("Error: .env file not found in the current directory.")  
    except Exception as e:  
        print(f"An unexpected error occurred: {e}")

    print(f"Successfully loaded confluence root link:")
    return links

def generate_confluence_md_files(output_dir,link,token,depth):
    jira_tickets = []
    confluence_client = ConfluenceClient(token)
    jira_tickets = confluence_client.get_page_content(link,True,output_dir, depth, prefix="",jira_tickets=jira_tickets)
    return jira_tickets

def generate_jira_md_files(output_dir,jira_tickets,jira_creds):

    jira_ticket_list = [item for sublist in jira_tickets for item in sublist]

    final_grouped_list = [[], [], []]

    base_urls = ["https://mlm.jira.samsungds.net/","http://sj-jira.ssi.samsung.com:8080/","https://ssirjirasw.samsungds.net/"]
    jira_tokens = [jira_creds["mlm_jira_token"],jira_creds["ted_jira_token"],jira_creds["ssir_jira_token"]]

    for link in jira_ticket_list:  
        # 2. Extract the domain (netloc) from the URL  
        parsed_url = urlparse(link)  
        domain = parsed_url.netloc
        ticket_id = link.split('/browse/')[-1]

        # 3. Assign to the specific index based on the domain name  
        if "mlm.jira.samsungds.net" in domain:  
            final_grouped_list[0].append(ticket_id)  
        elif "sj-jira.ssi.samsung.com" in domain:  
            final_grouped_list[1].append(ticket_id)  
        elif "ssirjirasw.samsungds.net" in domain:  
            final_grouped_list[2].append(ticket_id)

    for i in range(0,len(final_grouped_list)):
        generate_jira_md_file(output_dir,final_grouped_list[i],base_urls[i],jira_tokens[i])

def get_creds_from_env():
    jira_creds = {
        "mlm_jira_token" : os.getenv("mlm_jira_token"),
        "ssir_jira_token" : os.getenv("ssir_jira_token"),
        "ted_jira_token" : os.getenv("ted_jira_token"),
    }
    confluence_creds = {
        "token"    : os.getenv("confluence_token"),
    }
    return confluence_creds, jira_creds

      
##################################################################################################################################################
                # This code takes all confluence link as input and generate .md files and corresponding jira ticket details
##################################################################################################################################################

def run():

    links = read_root_confluence_link_from_env()
    confluence_creds, jira_creds = get_creds_from_env()
    output_dir=os.getenv("OUTPUT_DIR")
    depth=os.getenv("DEPTH")
    print("\n--------------------------Getting the confluence details----------------------------------\n")
    jira_tickets= generate_confluence_md_files(output_dir,links[0],confluence_creds["token"],int(depth))
    jira_fetch=os.getenv("FETCH_JIRA_INFO")
    if jira_fetch == "ON":
        print("\n--------------------------Getting the Jira details----------------------------------\n")
        generate_jira_md_files(output_dir,jira_tickets,jira_creds)
    else:
        print("\n--------------------------Jira Fetch configuration is OFF - Only confluence data fetched----------------------------------\n")
   

###############################################################################################################################
                                                        #main.py execution starts here
###############################################################################################################################

run()





