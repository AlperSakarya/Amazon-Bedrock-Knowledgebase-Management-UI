import streamlit as st
import boto3
import json
import time
import pandas as pd
from datetime import datetime
import pytz

eastern = pytz.timezone('US/Eastern')

# AWS Clients
s3_client = boto3.client("s3")
dynamodb_client = boto3.client("dynamodb")
bedrock_client = boto3.client("bedrock-agent-runtime")
cloudformation = boto3.client("cloudformation")  # For fetching S3 bucket names
lambda_client = boto3.client("lambda")
# For management/build-time operations (e.g., data source status)
bedrock_build_client = boto3.client("bedrock-agent")

##### YOU NEED TO UPDATE THIS VALUE WITH YOUR KB ID #####
bedrock_kb_id = "TJI3QHJ4XF"  # Bedrock Knowledge Base ID
bedrock_model_arn = "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-v2"  # Bedrock Model ARN

st.set_page_config(
    page_title="KB Operations",
    layout="wide",
    initial_sidebar_state="expanded",  # Ensure sidebar is expanded by default
)

def load_local_css(file_name):
    with open(file_name) as f:
        st.markdown(f'<style>{f.read()}</style>', unsafe_allow_html=True)

load_local_css("custom.css")

def get_s3_bucket_names():
    """Fetch both Intake and Approved S3 bucket names from CloudFormation stack outputs."""
    try:
        response = cloudformation.describe_stacks(StackName="brkb2")
        outputs = response["Stacks"][0]["Outputs"]

        intake_bucket = None
        approved_bucket = None

        for output in outputs:
            if output["OutputKey"] == "IntakeBucket":
                intake_bucket = output["OutputValue"]
            elif output["OutputKey"] == "KBApprovedBucket":
                approved_bucket = output["OutputValue"]

        if not intake_bucket or not approved_bucket:
            raise ValueError("One or both S3 bucket names could not be found in CloudFormation outputs.")

        return intake_bucket, approved_bucket

    except Exception as e:
        st.error(f"Error fetching S3 bucket names: {e}")
        return None, None

# Fetch S3 bucket names dynamically
intake_bucket_name, approved_bucket_name = get_s3_bucket_names()

# DynamoDB Table Name
DYNAMODB_TABLE_NAME = "changelogDB"
VERSION_TABLE_NAME = "versionCounter"

# --- Sidebar Navigation ---
st.sidebar.title("Navigation")

st.sidebar.markdown("### User Actions")
if st.sidebar.button("Home - KB Chat"):
    st.session_state.page = "Home"
if st.sidebar.button("View Documents"):
    st.session_state.page = "Document View"

st.sidebar.markdown("---")
st.sidebar.markdown("### Admin Actions")
if st.sidebar.button("KB Change Log and Versioning"):
    st.session_state.page = "Change Log"
if st.sidebar.button("KB Document Upload Portal"):
    st.session_state.page = "Document Upload"
if st.sidebar.button("KB Data Source Status"):
    st.session_state.page = "Data Source Status"

# Set a default page if none has been selected yet
if "page" not in st.session_state:
    st.session_state.page = "Home"

page = st.session_state.page

# --- Home Page (Chat with Bedrock Knowledge Base) ---
if page == "Home":
    st.title("Chat with the Knowledge Base")
    
    # Initialize conversation history if not already present
    if "messages" not in st.session_state:
        st.session_state.messages = []
    
    # Callback function to process the chat input when submitted
    def process_chat():
        user_text = st.session_state.chat_input  # Reads the widget's current value
        if user_text:
            # Append user's message
            st.session_state.messages.append({"role": "user", "content": user_text})
            prompt = f"Human: {user_text}\nAssistant:"
            try:
                response = bedrock_client.retrieve_and_generate(
                    input={"text": prompt},
                    retrieveAndGenerateConfiguration={
                        "type": "KNOWLEDGE_BASE",
                        "knowledgeBaseConfiguration": {
                            "knowledgeBaseId": bedrock_kb_id,
                            "modelArn": bedrock_model_arn
                        }
                    }
                )
                ai_response = response["output"]["text"]
            except Exception as e:
                ai_response = f"Error communicating with Bedrock: {str(e)}"
            # Append AI response
            st.session_state.messages.append({"role": "assistant", "content": ai_response})
    
    # Display conversation history using st.chat_message
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
    
    # Create the chat input widget with a callback on submit.
    st.chat_input("Type your question here...", key="chat_input", on_submit=process_chat)

# --- Data Source Status Page (Admin) ---
elif page == "Data Source Status":
    st.title("KB Data Source Status")
    st.write(f"Retrieving data source information for KB: {bedrock_kb_id}")
    try:
        # List data sources in the knowledge base
        list_ds_response = bedrock_build_client.list_data_sources(
            knowledgeBaseId=bedrock_kb_id,
            maxResults=100
        )
        ds_summaries = list_ds_response.get("dataSourceSummaries", [])
        st.write("---")
        # Create header row
        header_cols = st.columns([0.75, 2, 0.5, 1.5, 0.75, 1, 1])
        header_cols[0].write("DataSourceId")
        header_cols[1].write("Name")
        header_cols[2].write("Type")
        header_cols[3].write("Source Link")
        header_cols[4].write("Status")
        header_cols[5].write("Last Sync")
        header_cols[6].write("Actions")
        
        for summary in ds_summaries:
            ds_id = summary.get("dataSourceId")
            # Get detailed data source info
            ds_detail = bedrock_build_client.get_data_source(
                dataSourceId=ds_id,
                knowledgeBaseId=bedrock_kb_id
            ).get("dataSource", {})
            name = ds_detail.get("name", "")
            status = ds_detail.get("status", "")
            updatedAt = ds_detail.get("updatedAt")
            # Extract data source type from configuration
            ds_config = ds_detail.get("dataSourceConfiguration", {})
            ds_type = ds_config.get("type", "Unknown")
            source_link = ""
            if ds_type.upper() == "S3":
                s3_config = ds_config.get("s3Configuration", {})
                source_link = s3_config.get("bucketArn", "")
            elif ds_type.upper() == "WEB":
                web_config = ds_config.get("webConfiguration", {})
                src_conf = web_config.get("sourceConfiguration", {})
                url_conf = src_conf.get("urlConfiguration", {})
                seedUrls = url_conf.get("seedUrls", [])
                if seedUrls and isinstance(seedUrls, list):
                    source_link = seedUrls[0].get("url", "")
            # Retrieve sync/ingestion job info for this data source
            try:
                ingestion_response = bedrock_build_client.list_ingestion_jobs(
                    knowledgeBaseId=bedrock_kb_id,
                    dataSourceId=ds_id,
                    maxResults=10,
                    sortBy={
                            'attribute': 'STARTED_AT',
                            'order': 'DESCENDING'
                    }
                )
                ingestion_jobs = ingestion_response.get("ingestionJobSummaries", [])
                if ingestion_jobs:
                    job = ingestion_jobs[0]
                    job_status = job.get("status", "N/A")
                    if job_status.upper() == "COMPLETE":
                        updatedAt = job.get("updatedAt")
                        if updatedAt:
                            # Ensure updatedAt is a string
                            if isinstance(updatedAt, str):
                                # Parse the ISO 8601 formatted string into a datetime object
                                utc_time = datetime.fromisoformat(updatedAt.replace("Z", "+00:00"))
                            else:
                                # If updatedAt is not a string, attempt to convert it to a string
                                utc_time = datetime.fromisoformat(str(updatedAt).replace("Z", "+00:00"))

                            # Convert UTC time to US/Eastern time
                            est_time = utc_time.astimezone(eastern)

                            # Format the datetime object as 'YYYY-MM-DD HH:MM'
                            last_sync = est_time.strftime('%Y-%m-%d %H:%M')
                        else:
                            last_sync = "Unknown"
                    else:
                        last_sync = "Syncing..."
                else:
                    last_sync = "Never Synced"
            except Exception as sync_err:
                last_sync = f"Error: {str(sync_err)}"
            
            cols = st.columns([0.75, 2, 0.5, 1.5, 0.75, 1, 1])
            cols[0].write(ds_id)
            cols[1].write(name)
            cols[2].write(ds_type)
            if source_link:
                bucket_name = source_link.split(":::")[-1]
                cols[3].write(bucket_name)
            else:
                cols[3].write("")
            cols[4].write(status)
            cols[5].write(last_sync)
            # Action button: Initiate Sync
            if cols[6].button("Initiate Sync", key=f"sync-{ds_id}"):
                try:
                    # Trigger ingestion job for the data source
                    start_response = bedrock_build_client.start_ingestion_job(
                        knowledgeBaseId=bedrock_kb_id,
                        dataSourceId=ds_id
                    )
                    st.success(f"Ingestion job started for data source {ds_id}")
                except Exception as sync_start_err:
                    st.error(f"Failed to start ingestion job for {ds_id}: {sync_start_err}")
    except Exception as e:
        st.error(f"Error retrieving data source status: {str(e)}")


# --- Change Log Page (DynamoDB) ---
elif page == "Change Log":
    st.title("Knowledgebase Change Log")
    st.write("---")
    # Fetch the global version counter
    try:
        version_item = dynamodb_client.get_item(
            TableName=VERSION_TABLE_NAME,
            Key={"counter_id": {"S": "global"}}
        ).get("Item")
        global_counter = int(version_item.get("value", {}).get("N", "0")) if version_item else 0
    except Exception as e:
        st.error(f"Error fetching version counter: {e}")
        global_counter = 0

    latest_version = f"v1.{global_counter - 1}" if global_counter > 0 else "v1.0"

    # Fetch change log data
    try:
        response = dynamodb_client.scan(TableName=DYNAMODB_TABLE_NAME)
        files = response.get("Items", [])

        if not files:
            st.info("No records found in the change log.")
        else:
            # Prepare data for display
            data = []
            for file in files:
                data.append({
                    "File Name": file["file_name"]["S"],
                    "Timestamp": file["timestamp"]["N"],
                    "Status": file["status"]["S"],
                    "KB Version": file["kb_version"]["S"]
                })

            # Display table header
            col1, col2, col3, col4, col5 = st.columns([2, 2, 1, 1, 1])
            col1.write("**File Name**")
            col2.write("**Timestamp**")
            col3.write("**KB Version**")
            col4.write("**Status**")
            col5.write("**Action**")

            # Display data rows
            for record in data:
                col1, col2, col3, col4, col5 = st.columns([2, 2, 1, 1, 1])
                col1.write(record["File Name"])
                col2.write(record["Timestamp"])
                col3.write(record["KB Version"])
                col4.write(record["Status"])

                if record["Status"] == "New":
                    if col5.button("Approve", key=f"approve_{record['File Name']}"):
                        # Invoke the ApproveFileLambda
                        lambda_client.invoke(
                            FunctionName="KBApproveFile",
                            InvocationType="Event",
                            Payload=json.dumps({
                                "file_name": record["File Name"],
                                "timestamp": record["Timestamp"]
                            }),
                        )
                        st.success(f"Approved {record['File Name']}")
                elif record["Status"] == "Approved":
                    if record["KB Version"] == latest_version:
                        if col5.button("Rollback", key=f"rollback_{record['File Name']}"):
                            # Invoke the DeleteFileLambda
                            lambda_client.invoke(
                                FunctionName="KBDeleteFile",
                                InvocationType="Event",
                                Payload=json.dumps({
                                    "file_name": record["File Name"],
                                    "timestamp": record["Timestamp"]
                                }),
                            )
                            st.success(f"Rolled back {record['File Name']}")
                    else:
                        col5.write("—")
                else:
                    if col5.button("Delete", key=f"delete_{record['File Name']}"):
                        # Invoke the DeleteFileLambda
                        lambda_client.invoke(
                            FunctionName="KBDeleteFile",
                            InvocationType="Event",
                            Payload=json.dumps({
                                "file_name": record["File Name"],
                                "timestamp": record["Timestamp"]
                            }),
                        )
                        st.success(f"Deleted {record['File Name']}")

    except Exception as e:
        st.error(f"Failed to fetch change log: {str(e)}")

# --- Document Upload Page (Intake Bucket) ---
elif page == "Document Upload":
    st.title("Upload Documents")
    st.write("---")
    if not intake_bucket_name:
        st.error("Intake bucket name could not be retrieved from CloudFormation.")
    else:
        uploaded_file = st.file_uploader("Upload a document for review", type=["pdf", "txt", "docx"])
        if uploaded_file and st.button("Upload"):
            try:
                s3_client.upload_fileobj(uploaded_file, intake_bucket_name, uploaded_file.name)
                timestamp = int(time.time())
                dynamodb_client.put_item(
                    TableName=DYNAMODB_TABLE_NAME,
                    Item={
                        "file_name": {"S": uploaded_file.name},
                        "timestamp": {"N": str(timestamp)},
                        "status": {"S": "New"},
                        "kb_version": {"S": "pending"},
                    }
                )
                st.success(f"Uploaded {uploaded_file.name} successfully!")
            except Exception as e:
                st.error(f"Upload failed: {str(e)}")

    if not approved_bucket_name:
        st.error("Approved bucket name could not be retrieved from CloudFormation.")
    else:
        st.write("---")
        uploaded_file = st.file_uploader("Upload to Approved KB - skips HITL approval", type=["pdf", "txt", "docx"])
        if uploaded_file and st.button("Upload to KB"):
            try:
                s3_client.upload_fileobj(uploaded_file, approved_bucket_name, uploaded_file.name)
                st.success(f"Uploaded {uploaded_file.name} to Approved KB successfully!")
            except Exception as e:
                st.error(f"Upload to KB failed: {str(e)}")

# --- Document View Page (Display All Files from S3 Buckets) ---
elif page == "Document View":
    st.title("View Documents in Both Buckets")

    def list_s3_files(bucket_name):
        try:
            response = s3_client.list_objects_v2(Bucket=bucket_name)
            if "Contents" in response:
                return [{"File Name": obj["Key"], "Size (KB)": round(obj["Size"] / 1024, 2)} for obj in response["Contents"]]
            else:
                return [{"File Name": "No files found", "Size (KB)": "-"}]
        except Exception as e:
            st.error(f"Error accessing {bucket_name}: {str(e)}")
            return []

    if intake_bucket_name:
        st.subheader("📂 Intake Bucket")
        intake_files = list_s3_files(intake_bucket_name)
        st.table(intake_files)

    if approved_bucket_name:
        st.subheader("📂 Approved KB Bucket")
        approved_files = list_s3_files(approved_bucket_name)
        st.table(approved_files)
