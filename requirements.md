Building lightrag deployer for internal org needs, multiple projects needs thier own
lightrag hosted for thier project needs. We use lightrag from opensource.

[] lightrag dockercompose with patch
    - docker-compose.yml + patches included
    - check additional configuration for nginx enabled (since we planned to route each project hosted via specific url 
    example - http://mydomain.com/mmrag/p1/)
    - BASE_URL - http://mydomain.com
    - LIGHT_RAG_URL - http://mydomain.com/mmrag/p1/api/ 

[] Manage portal - web-portal service which helps managging ingestion of data
    - served at LIGHT_RAG_MGMT_URL - http://mydomain.com/mmrag/p1/mgmt (serves web portal)
    - connectors (they connect to data source like confluence, jira, github, etc..) - make it plugin kind of architecure 
    - direct documents ingestion (pdf, doc, md, txt, etc..)
    
    - user can provide configuration for each connector
        - confluence config (url, space, token, depth of ingestion, update freqeuncey (daily, week))
        - jira config (url, proj, token, depth of ingestion, update frequency(daily, week))
    - Ingestion pipeline - uses lightrag API, to ingest the data (user->mgmt->config->triger->ingest->lightrag->monitor)
        - display progress and stats on this portal
        - manage ingestion sessions
        - delete ingested data, clear, etc..
    - portal must have how to guide for each (confluenece, jira, etc..)
    - provide python package or source how to connect to confluence, jira and work lightrag API;s. we already have them in this manage portal, just add readme, source code for them to work with (packed right for re-use)
        - sample ingestion (connector + lightrag api)

[] script - per project deployment lightrag, manage portal, (setup.sh deploy <pro>, backup, restore, down), handle nginx configuration
    - dockerfile includes (manager portal, lightrag, rag datavase, neo4j, etc.., its own network)
    - must check pre-requisties before starting deployment
    - backup includes all (manager, config, rag db, neo4j, postgress, nginx, etc..)    
    - restore all of them, sanity check first


[] Simulated testing for development
    - confluence, jira, edm, etc..
    - with simulated docs

[] admin dashboard to manage projects - phase 2
    - deployments, check stats, schedule backup, restore, etc..
    - agent to monitor for any failures, fix, etc.. notify user via email..
    - docker hosted, it should be able to manage host network and deploy projects
    - each project links are listed here in this dashboard.

