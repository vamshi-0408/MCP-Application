import asyncio
from typing import Any, Dict, List, Optional, Union
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.server.elicitation import AcceptedElicitation,DeclinedElicitation,CancelledElicitation
from mcp.types import Tool, TextContent
import mcp.server.stdio
import os
from dotenv import load_dotenv
import json
import sys
import clr
import re
from concurrent.futures import ThreadPoolExecutor
import threading
import logging
import inspect
from azure.identity import DefaultAzureCredential
import struct
import sqlalchemy
import pyodbc
import pandas as pd
from datetime import datetime, timedelta
import requests
import anyio

# Setup logging with UTF-8 encoding
import io
import codecs

# Configure stdout and stderr for UTF-8 encoding
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stderr,
    encoding='utf-8',
    errors='replace'
)
logger = logging.getLogger(__name__)

# Load environment
load_dotenv()
dll_folder = os.getenv("Analysis_Services_path")
adomd_path = os.getenv("Adomd_DLL_Path")
clr.AddReference(os.path.join(dll_folder, "Microsoft.AnalysisServices.Tabular.dll"))
clr.AddReference(os.path.join(dll_folder, "Microsoft.AnalysisServices.Core.dll"))
clr.AddReference(os.path.join(adomd_path, "Microsoft.AnalysisServices.AdomdClient.dll"))

from Microsoft.AnalysisServices.Tabular import Server as TabularServer, Table as TabularTable, EntityPartitionSource, RefreshType, DataType, DataColumn, Partition as TabularPartition, ModeType  # type: ignore
from Microsoft.AnalysisServices.AdomdClient import AdomdSchemaGuid  # type: ignore
from Microsoft.AnalysisServices.AdomdClient import AdomdConnection, AdomdCommand  # type: ignore

class AuthenticationManager:
    """Centralized authentication manager for Azure tokens."""
    
    def __init__(self):
        self._access_token = None
        self._token_expiry = None
        self._credential = DefaultAzureCredential()
        self._lock = threading.Lock()
    
    def get_access_token(self, force_refresh: bool = False) -> str:
        """Get a valid access token, refreshing if necessary."""
        with self._lock:
            if (force_refresh or 
                self._access_token is None or 
                self._token_expiry is None or 
                datetime.now() >= self._token_expiry):
                
                token_result = self._credential.get_token("https://analysis.windows.net/powerbi/api/.default")
                self._access_token = token_result.token
                self._token_expiry = datetime.now() + timedelta(minutes=60)
                logger.info("Access token refreshed successfully")
            
            return self._access_token

class SQLEndpoint:
    """Handles SQL endpoint connections and queries with shared authentication."""
    
    def __init__(self):
        self.auth_manager = AuthenticationManager()
        self.engine = None
        self.sql_endpoint = None
        self.sql_database = None
        self.driver = None
        self.access_token = None
        self._connection_lock = threading.Lock()
    
    def initialize_sql_connection(self, sql_endpoint: str, sql_database: str):
        """Initialize the SQL engine with authentication and drivers."""
        with self._connection_lock:
            if not sql_endpoint or not sql_database:
                raise ValueError("sql_endpoint and sql_database must be provided")

            self.sql_endpoint = sql_endpoint
            self.sql_database = sql_database

            # Get available SQL Server drivers
            drivers = [d for d in pyodbc.drivers() if 'SQL Server' in d]
            if not drivers:
                raise RuntimeError("No SQL Server ODBC drivers found. Please install ODBC Driver for SQL Server.")

            # Prefer newer drivers
            preferred_drivers = ['ODBC Driver 18 for SQL Server', 'ODBC Driver 17 for SQL Server']
            self.driver = next((d for d in preferred_drivers if d in drivers), drivers[0])
            logger.info(f"Using driver: {self.driver}")

            if not self.access_token:
               self.access_token = self.auth_manager.get_access_token(force_refresh=True)

            # Prepare access token
            token_bytes = self.access_token.encode("utf-16-le")
            token_struct = struct.pack(f'<I{len(token_bytes)}s', len(token_bytes), token_bytes)
            SQL_COPT_SS_ACCESS_TOKEN = 1256

            # Build connection string
            connection_string = (
                f"Driver={{{self.driver}}};"
                f"Server={self.sql_endpoint},1433;"
                f"Database={self.sql_database};"
                f"Encrypt=Yes;"
                f"TrustServerCertificate=No;"
            )

            self.engine = sqlalchemy.create_engine(
                "mssql+pyodbc://",
                creator=lambda: pyodbc.connect(
                    connection_string,
                    attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct}
                )
            )
            return self.engine
    
    def get_sql_tables(self) -> pd.DataFrame:
        """Get SQL tables using the pre-authenticated SQL engine."""
        if not self.engine:
            raise Exception("Please provide SQL Endpoint Server and Database details.")
        
        df = pd.read_sql_query("SELECT name as table_name FROM sys.tables", self.engine)
        logger.info(f"Retrieved {len(df)} tables from SQL database")
        return df
    
    def execute_sql_query(self, query: str) -> pd.DataFrame:
        """Execute any SQL query using the pre-authenticated SQL engine."""
        if not self.engine:
            raise Exception("Please provide SQL Endpoint Server and Database details.")

        logger.info(f"Executing SQL query: {query[:100]}...")  # Log first 100 chars
        df = pd.read_sql_query(query, self.engine)
        logger.info(f"Query executed successfully, returned {len(df)} rows")
        return df
    
    def get_sql_table_schema(self, table_name: str) -> pd.DataFrame:
        """Get column information for a specific table."""
        query = f"""
        SELECT 
            COLUMN_NAME as column_name,
            DATA_TYPE as data_type
        FROM INFORMATION_SCHEMA.COLUMNS 
        WHERE TABLE_NAME = '{table_name}'
        ORDER BY ORDINAL_POSITION
        """
        return self.execute_sql_query(query)

class Fabric:
    """Handles Microsoft Fabric REST API operations with centralized authentication."""
    def __init__(self):
        self.auth_manager = AuthenticationManager()
        self.access_token = self.auth_manager.get_access_token()
    
    def get_workspace_info(self, workspace_identifier: str) -> Dict[str, Any]:
        if not self.access_token:
            self.access_token = self.auth_manager.get_access_token(force_refresh=True)
        info = None
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

        # Try to fetch by ID first
        url_by_id = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_identifier}"
        response = requests.get(url_by_id, headers=headers)

        if response.status_code == 200:
            data = response.json()
            info = {
                "workspace_id": data.get("id"),
                "workspace_name": data.get("displayName")
            }

        # If not found by ID, try searching by name
        url_list = f"https://api.fabric.microsoft.com/v1/workspaces/"
        response = requests.get(url_list, headers=headers)

        if response.status_code == 200:
            workspaces = response.json().get("value", [])
            for ws in workspaces:
                if ws.get("displayName", "").lower() == workspace_identifier.lower():
                    info = {
                        "workspace_id": ws.get("id"),
                        "workspace_name": ws.get("displayName")
                    }

        if not info:
            raise ValueError(f"Workspace '{workspace_identifier}' not found by ID or name.")
        return info
    
    def get_lakehouse_info(self, workspace_identifier: str, lakehouse_identifier: str) -> Dict[str, Any]:
        if not self.access_token:
            self.access_token = self.auth_manager.get_access_token(force_refresh=True)
        info = None
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        workspace_info = self.get_workspace_info(workspace_identifier)
        workspace_id = workspace_info.get("workspace_id", None)
        workspace_name = workspace_info.get("workspace_name", None)

        if not workspace_id:
            raise ValueError(f"Workspace '{workspace_identifier}' not found.")
        url_by_id = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/lakehouses/{lakehouse_identifier}"
        response = requests.get(url_by_id, headers=headers)

        if response.status_code == 200:
            data = response.json()
            info = {
                "workspace_id": workspace_id,
                "workspace_name": workspace_name,
                "lakehouse_id": data.get("id"),
                "lakehouse_name": data.get("displayName"),
                "sql_endpoint": data.get("properties",{}).get("sqlEndpointProperties",{}).get("connectionString",""),
                "sql_database":data.get("properties",{}).get("sqlEndpointProperties",{}).get("id","")
            }
        
        url_list = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/lakehouses"
        response = requests.get(url_list, headers=headers)

        if response.status_code == 200:
            lakehouses = response.json().get("value", [])
            for lh in lakehouses:
                if lh.get("displayName", "").lower() == lakehouse_identifier.lower():
                    info = {
                        "workspace_id": workspace_id,
                        "workspace_name": workspace_name,
                        "lakehouse_id": lh.get("id"),
                        "lakehouse_name": lh.get("displayName"),
                        "sql_endpoint": lh.get("properties",{}).get("sqlEndpointProperties",{}).get("connectionString",""),
                        "sql_database":lh.get("properties",{}).get("sqlEndpointProperties",{}).get("id","")
                    }

        if not info:
            raise ValueError(f"Lakehouse '{lakehouse_identifier}' not found by ID or name.")

        return info
    
    def create_lakehouse(self, workspace_identifier: str, lakehouse_name: str, description: str = None) -> Dict[str, Any]:
        """Create a new lakehouse using Fabric REST API"""
        try:
            if not self.access_token:
               self.access_token = self.auth_manager.get_access_token(force_refresh=True)
            
            workspace_id=self.get_workspace_info(workspace_identifier).get("workspace_id",None)
            if not workspace_id:
                raise ValueError(f"Workspace '{workspace_identifier}' not found.")
            
            endpoint = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/lakehouses"
            
            body = {
                "displayName": lakehouse_name,
                "description": description
            }

            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json"
            }

            response = requests.post(endpoint, headers=headers, json=body)
            return response.text
        
        except Exception as e:
            return f"{str(e)}"

    async def create_lakehouse_shortcut(self, target_workspace: str = None, target_lakehouse: str = None, target_shortcut_path: str = None, target_shortcut_name: str = None, source_workspace: str = None, source_lakehouse: str = None, source_path: str = None, approved: bool = False) -> dict:
        """Creating shortcuts from authoritative workspace and lakehouse into target workspace, target lakehouse and target path with MCP elicitation for approval."""
        try:
            # If no parameters provided, return elicitation prompt to collect all details
            if not all([target_workspace, target_lakehouse, target_shortcut_path, target_shortcut_name, source_workspace, source_lakehouse, source_path]):
                approval_prompt = """
Create Lakehouse Shortcut

Please provide the following information for creating the lakehouse shortcut:

1. Target Workspace: Name or ID of the target workspace
2. Target Lakehouse: Name or ID of the target lakehouse  
3. Target Shortcut Path: Path in target lakehouse (e.g., 'Tables' or 'Files/folder')
4. Target Shortcut Name: Name for the shortcut
5. Source Workspace: Name or ID of the source workspace
6. Source Lakehouse: Name or ID of the source lakehouse
7. Source Path: Path in source lakehouse (e.g., 'Tables/table_name' or 'Files/folder')

After providing these details, you will be asked for final approval before creating the shortcut.
                """

                return {
                    "type": "elicitation_required",
                    "prompt": approval_prompt.strip(),
                    "properties": {
                        "target_workspace": {"type": "string", "description": "Name or ID of the target workspace"},
                        "target_lakehouse": {"type": "string", "description": "Name or ID of the target lakehouse"},
                        "target_shortcut_path": {"type": "string", "description": "Path in target lakehouse (e.g., 'Tables' or 'Files/folder')"},
                        "target_shortcut_name": {"type": "string", "description": "Name for the shortcut"},
                        "source_workspace": {"type": "string", "description": "Name or ID of the source workspace"},
                        "source_lakehouse": {"type": "string", "description": "Name or ID of the source lakehouse"},
                        "source_path": {"type": "string", "description": "Path in source lakehouse (e.g., 'Tables/table_name' or 'Files/folder')"}
                    },
                    "required_properties": ["target_workspace", "target_lakehouse", "target_shortcut_path", "target_shortcut_name", "source_workspace", "source_lakehouse", "source_path"]
                }

            # Validate paths
            if target_shortcut_path.lower() == "tables":
                pass
            elif target_shortcut_path.lower().startswith("files/"):
                pass
            else:
                raise ValueError("Invalid target shortcut path. It should be either 'Tables' or start with 'Files'.")
            
            if source_path.lower().startswith("tables/"):
                pass
            elif source_path.lower().startswith("files/"):
                pass
            else:
                raise ValueError("Invalid source path. It should start with 'Tables/' or start with 'Files/'.")

            # Get workspace and lakehouse information
            target_lakehouse_info = self.get_lakehouse_info(target_workspace, target_lakehouse)
            target_lakehouse_id = target_lakehouse_info.get("lakehouse_id")
            target_lakehouse_name = target_lakehouse_info.get("lakehouse_name")
            target_workspace_id = target_lakehouse_info.get("workspace_id")
            target_workspace_name = target_lakehouse_info.get("workspace_name")
            
            source_lakehouse_info = self.get_lakehouse_info(source_workspace, source_lakehouse)
            source_lakehouse_id = source_lakehouse_info.get("lakehouse_id")
            source_lakehouse_name = source_lakehouse_info.get("lakehouse_name")
            source_workspace_id = source_lakehouse_info.get("workspace_id")
            source_workspace_name = source_lakehouse_info.get("workspace_name")

            # Prepare request body
            request_body = {
                "path": target_shortcut_path,
                "name": target_shortcut_name,
                "target": {
                    "oneLake": {
                        "workspaceId": source_workspace_id,
                        "itemId": source_lakehouse_id,
                        "path": source_path
                    }
                }
            }

            # If not approved, return elicitation prompt for approval
            if not approved:
                approval_prompt = f"""
Create Lakehouse Shortcut - Final Approval Required

Review the following shortcut details:

Source:
- Workspace: {source_workspace_name} (ID: {source_workspace_id})
- Lakehouse: {source_lakehouse_name} (ID: {source_lakehouse_id})
- Path: {source_path}

Target:
- Workspace: {target_workspace_name} (ID: {target_workspace_id})
- Lakehouse: {target_lakehouse_name} (ID: {target_lakehouse_id})
- Shortcut Path: {target_shortcut_path}
- Shortcut Name: {target_shortcut_name}

This operation will create a shortcut linking the source data to the target location.
Do you approve creating this shortcut?

Reply with 'yes' to approve or 'no' to cancel.
                """

                return {
                    "type": "elicitation_required",
                    "prompt": approval_prompt.strip(),
                    "request_body": request_body,
                    "target_workspace_id": target_workspace_id,
                    "target_lakehouse_id": target_lakehouse_id,
                    "properties": {
                        "target_workspace": {"type": "string", "description": "Name or ID of the target workspace"},
                        "target_lakehouse": {"type": "string", "description": "Name or ID of the target lakehouse"},
                        "target_shortcut_path": {"type": "string", "description": "Path in target lakehouse (e.g., 'Tables' or 'Files/folder')"},
                        "target_shortcut_name": {"type": "string", "description": "Name for the shortcut"},
                        "source_workspace": {"type": "string", "description": "Name or ID of the source workspace"},
                        "source_lakehouse": {"type": "string", "description": "Name or ID of the source lakehouse"},
                        "source_path": {"type": "string", "description": "Path in source lakehouse (e.g., 'Tables/table_name' or 'Files/folder')"},
                        "approved": {"type": "boolean", "description": "Final approval confirmation", "default": False}
                    },
                    "required_properties": ["target_workspace", "target_lakehouse", "target_shortcut_path", "target_shortcut_name", "source_workspace", "source_lakehouse", "source_path", "approved"],
                    "source_info": {
                        "workspace": source_workspace_name,
                        "lakehouse": source_lakehouse_name,
                        "path": source_path
                    },
                    "target_info": {
                        "workspace": target_workspace_name,
                        "lakehouse": target_lakehouse_name,
                        "shortcut_path": target_shortcut_path,
                        "shortcut_name": target_shortcut_name
                    }
                }

            # If approved, proceed with shortcut creation
            if not self.access_token:
                self.access_token = self.auth_manager.get_access_token(force_refresh=True)

            url = f"https://api.fabric.microsoft.com/v1/workspaces/{target_workspace_id}/items/{target_lakehouse_id}/shortcuts?shortcutConflictPolicy=Abort"

            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json"
            }

            response = requests.post(url, headers=headers, json=request_body, timeout=30)

            if response.status_code == 201:
                return {
                    "success": "Shortcut created successfully", 
                    "response": response.json(),
                    "shortcut_details": {
                        "source": f"{source_workspace_name}/{source_lakehouse_name}/{source_path}",
                        "target": f"{target_workspace_name}/{target_lakehouse_name}/{target_shortcut_path}/{target_shortcut_name}"
                    }
                }
            else:
                return {"error": f"Failed to create shortcut: {response.status_code} - {response.text}"}

        except Exception as e:
            return {"error": str(e)}

class TabularEditor:
    def __init__(self):
        self.connection_string = None
        self.connected = False
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.model = None
        self.connection_lock = threading.Lock()
        self.tabularserver = TabularServer()
        self.fabric = Fabric()
        self.sql_metadata = SQLEndpoint()   

    def connect_dataset(self, workspace_identifier: str, database_name: str) -> bool:
        """Establish connection to Power BI dataset"""
        try: 
            workspace_name = self.fabric.get_workspace_info(workspace_identifier).get("workspace_name", None)
            if not workspace_name:
                raise ValueError(f"Workspace '{workspace_identifier}' not found.")
            self.connection_string = (
            f"Provider=MSOLAP;"
            f"Data Source=powerbi://api.powerbi.com/v1.0/myorg/{workspace_name};"
            f"Initial Catalog={database_name};"
            f"User ID={os.getenv('User_ID')};"
            f"Password={os.getenv('Password')};"
            )
            self.tabularserver.Connect(self.connection_string)
            isexists = False
            for database in self.tabularserver.Databases:
                if database.Name == database_name:
                     db = database
                     isexists = True
                     break
            if not isexists:
                raise f"{database_name} not found in the provided server."

            self.model = db.Model
            self.connected = True
            logger.info(f"✅ Connected to model '{db.Name}'.")
        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            raise Exception(f"Connection failed: {str(e).encode('ascii', 'replace').decode('ascii')}")

    def disconnect_dataset(self):
        self.tabularserver.Disconnect()
        logger.info("Disconnected from server.")
        return "Disconnected from server is successfull"
    
    def list_tables(self) -> List[str]:
        """List all tables in the connected model."""
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        return [t.Name for t in self.model.Tables]

    def get_multiple_sql_tables_schema(self, table_names: List[str]) -> Dict[str, List[Dict[str, str]]]:
        """Helper function to get schema information for multiple sql tables."""
        tables_schema = {}
        for table_name in table_names:
            try:
                schema_df = self.sql_metadata.get_sql_table_schema(table_name)
                tables_schema[table_name] = [
                    {
                        "column_name": row["column_name"],
                        "data_type": row["data_type"]
                    }
                    for _, row in schema_df.iterrows()
                ]
                logger.info(f"Retrieved schema for table: {table_name} with {len(tables_schema[table_name])} columns")
            except Exception as e:
                error_msg = str(e).encode('ascii', 'replace').decode('ascii')
                logger.error(f"Error retrieving schema for table {table_name}: {error_msg}")
                tables_schema[table_name] = []
        return tables_schema
    
    def generate_tmsl_columns(self, columns_schema: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        """Helper function to generate TMSL column definitions from schema information."""
        tmsl_columns = []
        
        # SQL to Analysis Services data type mapping
        type_mapping = {
            'bigint': 'int64',
            'int': 'int64',
            'smallint': 'int64',
            'tinyint': 'int64',
            'bit': 'boolean',
            'decimal': 'decimal',
            'numeric': 'decimal',
            'money': 'decimal',
            'smallmoney': 'decimal',
            'float': 'double',
            'real': 'double',
            'datetime': 'dateTime',
            'datetime2': 'dateTime',
            'smalldatetime': 'dateTime',
            'date': 'dateTime',
            'time': 'dateTime',
            'datetimeoffset': 'dateTime',
            'char': 'string',
            'varchar': 'string',
            'text': 'string',
            'nchar': 'string',
            'nvarchar': 'string',
            'ntext': 'string',
            'binary': 'binary',
            'varbinary': 'binary',
            'image': 'binary',
            'uniqueidentifier': 'string'
        }
        
        for column in columns_schema:
            column_name = column['column_name']
            sql_data_type = column['data_type'].lower()
            
            # Map SQL data type to Analysis Services data type
            as_data_type = type_mapping.get(sql_data_type, 'string')
            
            column_def = {
                "name": column_name,
                "dataType": as_data_type,
                "sourceColumn": column_name
            }
            
            tmsl_columns.append(column_def)
        
        return tmsl_columns
    
    def select_tables_with_schema(self, selected_table_names: List[str] = None) -> Dict[str, Any]:
        """Select specific tables and return their schemas, or return all tables if none specified."""
        available_tables_df = self.sql_metadata.get_sql_tables()
        available_tables = available_tables_df['table_name'].tolist()
        
        if selected_table_names:
            # Validate that all selected tables exist
            invalid_tables = [table for table in selected_table_names if table not in available_tables]
            if invalid_tables:
                raise ValueError(f"The following tables do not exist: {invalid_tables}")
            tables_to_process = selected_table_names
        else:
            tables_to_process = available_tables
        
        logger.info(f"Processing {len(tables_to_process)} tables: {tables_to_process}")
        tables_with_schema = self.get_multiple_sql_tables_schema(tables_to_process)
        
        return {
            "available_tables": available_tables,
            "selected_tables": tables_to_process,
            "tables_schema": tables_with_schema
        }
    
    def create_semantic_model(self, workspace_identifier: str, lakehouse_identifier:str ,semantic_model_name: str, selected_tables: List[str] = None, description: str = None) -> Dict[str, Any]:
        """Create a comprehensive DirectLake semantic model using TMSL for full DAX Studio and XMLA support with automatic refresh"""
        
        # IMMEDIATE TEST - This should appear in logs if our code is running
        print("🔥🔥🔥 VAMSHI CODE IS EXECUTING 🔥🔥🔥")
        logger.error("🔥🔥🔥 VAMSHI CODE IS EXECUTING 🔥🔥🔥")
        
        try:
            # Debug: Log input parameters
            debug_start = {
                "workspace_identifier": workspace_identifier,
                "lakehouse_identifier": lakehouse_identifier,
                "semantic_model_name": semantic_model_name,
                "selected_tables": selected_tables,
                "description": description
            }
            print("="*80)
            print("🚀 SEMANTIC MODEL CREATION - STEP BY STEP")
            print("="*80)
            print(f"📋 Parameters: workspace={workspace_identifier}, lakehouse={lakehouse_identifier}, model={semantic_model_name}, tables={selected_tables}")
            
            print("📍 STEP 1: Getting lakehouse info...")
            logger.info("="*80)
            logger.info("🚀 SEMANTIC MODEL CREATION - STEP BY STEP")
            logger.info("="*80)
            logger.info(f"📋 Parameters: workspace={workspace_identifier}, lakehouse={lakehouse_identifier}, model={semantic_model_name}, tables={selected_tables}")
            
            logger.info("📍 STEP 1: Getting lakehouse info...")
            try:
                lakehouse_info = self.fabric.get_lakehouse_info(workspace_identifier,lakehouse_identifier)
                print(f"✅ STEP 1 SUCCESS: {lakehouse_info}")
                logger.info(f"✅ STEP 1 SUCCESS: {lakehouse_info}")
            except Exception as e:
                print(f"❌ STEP 1 FAILED: {str(e)}")
                logger.error(f"❌ STEP 1 FAILED: {str(e)}")
                return {"success": False, "error": f"Step 1 failed - lakehouse info: {str(e)}", "debug_start": debug_start}
                
            logger.info("Step 2: Extracting lakehouse properties...")
            try:
                workspace_name = lakehouse_info.get("workspace_name") if lakehouse_info else None
                workspace_id = lakehouse_info.get("workspace_id") if lakehouse_info else None
                lakehouse_name = lakehouse_info.get("lakehouse_name") if lakehouse_info else None
                lakehouse_sql_endpoint = lakehouse_info.get("sql_endpoint") if lakehouse_info else None
                lakehouse_database = lakehouse_info.get("sql_database") if lakehouse_info else None
                logger.info(f"Extracted: workspace_name={workspace_name}, lakehouse_name={lakehouse_name}")
            except Exception as e:
                logger.error(f"Step 2 failed: {str(e)}")
                return {"success": False, "error": f"Failed to extract lakehouse properties: {str(e)}", "lakehouse_info": lakehouse_info}
            
            logger.info(f"Lakehouse info retrieved: {lakehouse_info}")

            if not lakehouse_sql_endpoint:
                return {"success": False, "error": f"Lakehouse endpoint '{lakehouse_sql_endpoint}' not found."}

            logger.info(f"Creating semantic model '{semantic_model_name}' in lakehouse '{lakehouse_name}'...")
            logger.info(f"SQL Endpoint: '{lakehouse_sql_endpoint}'")
            logger.info(f"SQL Database: '{lakehouse_database}'")

            # Initialize SQL metadata if endpoint information is provided
            tables_info = {}
            
            if lakehouse_sql_endpoint and lakehouse_database and lakehouse_sql_endpoint.strip() and lakehouse_database.strip():
                logger.info("Step 3: Initializing SQL connection...")
                try:
                    self.sql_metadata.initialize_sql_connection(lakehouse_sql_endpoint, lakehouse_database)
                    logger.info("SQL connection initialized successfully")
                except Exception as e:
                    logger.error(f"Step 3 failed: {str(e)}")
                    return {"success": False, "error": f"Failed to initialize SQL connection: {str(e)}"}
                
                logger.info(f"Step 4: Getting table schema for selected_tables: {selected_tables}")
                try:
                    tables_info = self.select_tables_with_schema(selected_tables)
                    logger.info(f"tables_info type: {type(tables_info)}, keys: {tables_info.keys() if tables_info else 'None'}")
                except Exception as e:
                    logger.error(f"Step 4 failed: {str(e)}")
                    return {"success": False, "error": f"Failed to get table schema: {str(e)}"}
                
                if tables_info and 'selected_tables' in tables_info:
                    logger.info(f"Retrieved schema for {len(tables_info['selected_tables'])} tables")
                else:
                    return {"success": False, "error": "Failed to retrieve table schema information", "tables_info": tables_info}
            else:
                return {"success": False, "error": "UNIQUE_ERROR_2025_v2: Please provide valid lakehouse SQL endpoint and database details.", "received_endpoint": lakehouse_sql_endpoint, "received_database": lakehouse_database}

            # Connect to workspace via XMLA
            xmla_connection = f"powerbi://api.powerbi.com/v1.0/myorg/{workspace_name}"
            logger.info(f"Connecting to XMLA endpoint: {xmla_connection}")
            connection_string = (
                    f"Provider=MSOLAP;"
                    f"Data Source={xmla_connection};"
                    f"User ID={os.getenv('USER_ID')};"
                    f"Password={os.getenv('PASSWORD')};"
                )
            
            server = TabularServer()
            server.Connect(connection_string)
            logger.info("Successfully connected to Analysis Services")
            
            # Generate tables for TMSL command
            logger.info("Step 5: Generating TMSL tables...")
            tmsl_tables = []
            logger.info(f"tables_info keys: {list(tables_info.keys()) if tables_info else 'None'}")
            
            if tables_info and 'tables_schema' in tables_info:
                logger.info(f"Found tables_schema with {len(tables_info['tables_schema'])} tables")
                for table_name, columns in tables_info['tables_schema'].items():
                    logger.info(f"Processing table '{table_name}' with {len(columns) if columns else 0} columns")
                    if columns:  # Only add tables that have columns
                        try:
                            tmsl_columns = self.generate_tmsl_columns(columns)
                            logger.info(f"Generated {len(tmsl_columns)} TMSL columns for table '{table_name}'")
                            
                            table_def = {
                                "name": table_name,
                                "columns": tmsl_columns,
                                "partitions": [
                                    {
                                        "name": f"{table_name}",
                                        "mode": "directLake",
                                        "source": {
                                            "type": "entity",
                                            "entityName": table_name,
                                            "expressionSource": "DatabaseQuery"
                                        }
                                    }
                                ]
                            }
                            tmsl_tables.append(table_def)
                            logger.info(f"✅ Successfully added table '{table_name}' with {len(columns)} columns to model")
                        except Exception as e:
                            logger.error(f"❌ Failed to process table '{table_name}': {str(e)}")
                    else:
                        logger.warning(f"⚠️  Skipping table '{table_name}' - no columns found")
            else:
                logger.error("❌ No tables_schema found in tables_info")
            
            logger.info(f"Step 5 Complete: Generated {len(tmsl_tables)} tables for TMSL command")
            
            # Use proper TMSL Create command structure
            logger.info("Step 6: Creating TMSL command structure...")
            tmsl_create_command = {
                "createOrReplace": {
                    "object": {
                        "database": semantic_model_name
                    },
                    "database": {
                        "name": semantic_model_name,
                        "compatibilityLevel": 1604,
                        "model": {
                            "culture": "en-US",
                            "collation": "Latin1_General_100_BIN2_UTF8",
                            "dataAccessOptions": {
                                "legacyRedirects": True,
                                "returnErrorValuesAsNull": True
                            },
                            "defaultPowerBIDataSourceVersion": "powerBI_V3",
                            "sourceQueryCulture": "en-US",
                            "directLakeBehavior": "directLakeOnly",
                            "tables": tmsl_tables,  # This is where our tables go
                            "cultures": [
                                {
                                    "name": "en-US",
                                    "linguisticMetadata": {
                                        "content": {
                                            "Version": "1.0.0",
                                            "Language": "en-US"
                                        },
                                        "contentType": "json"
                                    }
                                }
                            ],
                            "expressions": [
                                {
                                    "name": "DatabaseQuery",
                                    "kind": "m",
                                    "expression": f"let\n    database = Sql.Database(\"{lakehouse_sql_endpoint}\", \"{lakehouse_database}\")\nin\n    database" if lakehouse_sql_endpoint and lakehouse_database else "let\n    source = #\"Empty Table\"\nin\n    source",
                                    "annotations": [
                                        {
                                            "name": "PBI_IncludeFutureArtifacts",
                                            "value": "False"
                                        }
                                    ]
                                }
                            ],
                            "annotations": [
                                {
                                    "name": "__PBI_TimeIntelligenceEnabled",
                                    "value": "0"
                                },
                                {
                                    "name": "PBIDesktopVersion",
                                    "value": "2.147.7761.2 (Main)+4fed62a961b3b674388272b595c56068c5925805"
                                },
                                {
                                    "name": "PBI_QueryOrder",
                                    "value": "[\"DatabaseQuery\"]"
                                },
                                {
                                    "name": "PBI_ProTooling",
                                    "value": "[\"WebModelingEdit\"]"
                                }
                            ]
                        }
                    }
                }
            }
            
            logger.info(f"TMSL command created with {len(tmsl_tables)} tables in model structure")
            logger.info("Step 6 Complete: TMSL command structure ready")
            
            logger.info("Executing TMSL command to create semantic model...")
            # Execute the TMSL command
            result = server.Execute(json.dumps(tmsl_create_command))
            logger.info(f"TMSL execution result: {result}")
                       # Wait a moment and try to verify the model was created
            import time
            time.sleep(10)  # Give model time to be available
            server.Refresh()
        
            
            # Check if our model exists and automatically refresh it
            created_model = server.Databases.Find(semantic_model_name)
            if created_model:
                logger.info(f"Successfully verified model '{semantic_model_name}' was created")
                
                # Automatically refresh the model immediately after creation
                if tmsl_tables:  # Only refresh if there are tables
                    logger.info("Starting Automatic Refresh")
                    try:
                        created_model.Model.RequestRefresh(RefreshType.Full)
                        created_model.Model.SaveChanges()
                        logger.info("Model refreshed successfully immediately after creation")
                        refresh_success = True
                        refresh_message = "Model created and refreshed successfully"
                    except Exception as refresh_error:
                        refresh_error_msg = str(refresh_error).encode('ascii', 'replace').decode('ascii')
                        logger.warning(f"Refresh failed: {refresh_error_msg}")
                        refresh_success = False
                        refresh_message = f"Model created but refresh failed: {str(refresh_error)}"
                else:
                    refresh_success = True
                    refresh_message = "Model created successfully (no tables to refresh)"
            else:
                logger.warning(f"Model '{semantic_model_name}' not found immediately after creation")
                refresh_success = False
                refresh_message = "Model creation status unclear"
            
            server.Disconnect()
            logger.info("Disconnected from Analysis Services")
            
            # Prepare the result
            creation_result = {
                "success": True, 
                "message": refresh_message,
                "workspace_id": workspace_id,
                "workspace_name": workspace_name,
                "model_name": semantic_model_name,
                "tables_added": [table['name'] for table in tmsl_tables],
                "total_tables": len(tmsl_tables),
                "refresh_success": refresh_success
            }
            self.refresh_semantic_model(workspace_id, semantic_model_name)
            return creation_result
            
        except Exception as e:
            error_msg = str(e).encode('ascii', 'replace').decode('ascii')
            logger.error(f"Error creating semantic model: {error_msg}")
            return {"success": False, "error": error_msg}

    def refresh_semantic_model(self, workspace_identifier: str, semantic_model_name: str, refresh_type: str = "Full") -> Dict[str, Any]:
        """Refresh a semantic model with the specified parameters"""
        try:
            logger.info(f"🔄 Starting {refresh_type} refresh for '{semantic_model_name}'...")
            if refresh_type == "Full":
                refresh_type = RefreshType.Full
                pass
            else:
                refresh_type = RefreshType.Automatic
            # Connect to the dataset if not already connected
            if not self.connected:
                self.connect_dataset(workspace_identifier, semantic_model_name)
            
            # Perform the refresh operation
            if self.model and self.connected:
                # Request refresh on the model
                self.model.RequestRefresh(refresh_type)
                self.model.SaveChanges()
                
                print(f"✅ Refresh request submitted for '{semantic_model_name}'")
                
                
                return {
                    "success": True,
                    "message": f"Successfully refreshed '{semantic_model_name}'",
                    "refresh_type": str(refresh_type),
                    "workspace": workspace_identifier,
                    "model_name": semantic_model_name
                }
            else:
                return {
                    "success": False,
                    "error": "Model not connected. Please connect to the dataset first.",
                    "connected": self.connected,
                    "model_available": self.model is not None
                }
                
        except Exception as e:
            error_msg = str(e).encode('ascii', 'replace').decode('ascii')
            logger.error(f"Refresh failed for '{semantic_model_name}': {error_msg}")
            return {
                "success": False,
                "error": f"Refresh failed: {error_msg}",
                "workspace": workspace_identifier,
                "model_name": semantic_model_name
            }
    
    def execute_dax_query(self, dax_query: str) -> List[Dict[str, Any]]:
        """Execute a DAX query using AdomdClient"""
        if not self.connection_string:
            raise Exception("Not connected to Power BI.")
        logger.info(f"Executing DAX query:\n{dax_query}")
        results = []
        try:
            conn = AdomdConnection(self.connection_string)
            conn.Open()
            cmd = AdomdCommand(dax_query, conn)
            reader = cmd.ExecuteReader()
            columns = [reader.GetName(i) for i in range(reader.FieldCount)]
            while reader.Read():
                row = {columns[i]: reader.GetValue(i) for i in range(len(columns))}
                results.append(row)
            reader.Close()
            conn.Close()
            logger.info(f"Returned {len(results)} rows.")
            return results
        except Exception as e:
            logger.error(f"DAX query execution failed: {str(e)}")
            raise Exception(f"DAX query execution failed: {str(e)}")
        
    def create_measure(self, table_name: str, measure_name: str, dax_expression: str):
        logger.info(f"Attempting to create measure '{measure_name}' in table '{table_name}' with DAX expression: {dax_expression}")
        if not self.connected:
            raise Exception("Tabular server is not connected")

        # Find the table (case-insensitive)
        table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
        if not table:
            logger.error(f"Table '{table_name}' not found in the model.")
            raise Exception(f"Table '{table_name}' not found in the model.")

        # Check if the measure already exists (case-insensitive)
        if any(m.Name.lower() == measure_name.lower() for m in table.Measures):
            logger.error(f"Measure '{measure_name}' already exists in table '{table.Name}'.")
            raise Exception(f"❌ Measure '{measure_name}' already exists in table '{table.Name}'.")

        # Create and add the new measure
        logger.info(f"Creating measure '{measure_name}' in table '{table.Name}'...")
        try:
            new_measure = table.AddMeasure(measure_name, dax_expression, "")
            self.model.SaveChanges()
            logger.info(f"✅ Measure '{measure_name}' created successfully in table '{table.Name}'")
            return f"✅ Measure '{measure_name}' created successfully in table '{table.Name}'"
        except Exception as e:
            logger.error(f"❌ Failed to create measure '{measure_name}' in table '{table.Name}': {str(e)}")
            raise Exception(f"❌ Failed to create measure '{measure_name}' in table '{table.Name}': {str(e)}")

    def list_all_relationships(self) -> Dict[str, Any]:
        """List all relationships and include the count."""
        if not self.connected:
            raise Exception("Tabular server is not connected")
            
        relationships = []
        for rel in self.model.Relationships:
            rel_id = getattr(rel, 'Name', None) or getattr(rel, 'ID', None)
            relationships.append({
                "from_table": rel.FromTable.Name,
                "from_column": rel.FromColumn.Name,
                "to_table": rel.ToTable.Name,
                "to_column": rel.ToColumn.Name,
                "relationship_id": rel_id
            })
        logger.info(f"Found {len(relationships)} relationships in total.")
        return {"relationships": relationships, "count": len(relationships)}
    
    def add_directlake_table(self, source_table: str, table_name: Optional[str] = None) -> str:
        """Add a DirectLake table to the model using SQL endpoint schema and auto-refresh."""
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        
        if not self.sql_metadata.engine:
            raise Exception("SQL endpoint not initialized. Call initialize_connection() first.")
        
        # Use source_table as table_name if not provided
        if table_name is None:
            table_name = source_table
        
        try:
            logger.info(f"Starting DirectLake table creation: {table_name} from {source_table}")
            
            # First verify the source table exists in SQL endpoint
            schema_df = self.sql_metadata.get_table_schema(source_table)
            
            if schema_df.empty:
                raise Exception(f"Source table '{source_table}' not found in lakehouse. Please verify the table name exists in the SQL endpoint.")
            
            # Then check if table already exists in PowerBI model
            existing_table = next((table for table in self.model.Tables if table.Name == table_name), None)
            
            if existing_table:
                raise Exception(f"Table '{table_name}' already exists in the PowerBI model. Please choose a different name or remove the existing table first.")
            
            # Create new DirectLake table
            new_table = TabularTable()
            new_table.Name = table_name
            
            # Create partition with DirectLake configuration
            partition = TabularPartition()
            partition.Name = table_name
            partition.Mode = ModeType.DirectLake  # Set DirectLake mode explicitly
            
            # Create entity partition source
            entity_source = EntityPartitionSource()
            entity_source.EntityName = source_table
            
            # Set the expression source to the lakehouse connection
            if hasattr(self.model, 'Expressions') and self.model.Expressions.Count > 0:
                first_expression = next(iter(self.model.Expressions), None)
                if first_expression:
                    entity_source.ExpressionSource = first_expression
                else:
                    logger.warning("No expressions found - DirectLake may not work properly without proper lakehouse connection")
            else:
                logger.warning("No named expressions found - DirectLake may not work properly without proper lakehouse connection")
            
            partition.Source = entity_source
            new_table.Partitions.Add(partition)
            
            # Add table to model first
            self.model.Tables.Add(new_table)
            
            # Add columns based on SQL schema
            for _, row in schema_df.iterrows():
                try:
                    col_name = row['column_name']
                    sql_data_type = row['data_type'].lower()
                    
                    # Map SQL data types to Tabular DataType
                    data_type_mapping = {
                        'varchar': DataType.String,
                        'nvarchar': DataType.String,
                        'char': DataType.String,
                        'nchar': DataType.String,
                        'text': DataType.String,
                        'ntext': DataType.String,
                        'int': DataType.Int64,
                        'bigint': DataType.Int64,
                        'smallint': DataType.Int64,
                        'tinyint': DataType.Int64,
                        'bit': DataType.Boolean,
                        'decimal': DataType.Decimal,
                        'numeric': DataType.Decimal,
                        'float': DataType.Double,
                        'real': DataType.Double,
                        'money': DataType.Decimal,
                        'smallmoney': DataType.Decimal,
                        'datetime': DataType.DateTime,
                        'datetime2': DataType.DateTime,
                        'smalldatetime': DataType.DateTime,
                        'date': DataType.DateTime,
                        'time': DataType.DateTime,
                        'timestamp': DataType.DateTime,
                        'uniqueidentifier': DataType.String,
                        'binary': DataType.Binary,
                        'varbinary': DataType.Binary,
                        'image': DataType.Binary
                    }
                    
                    # Get mapped data type or default to String
                    tabular_data_type = data_type_mapping.get(sql_data_type, DataType.String)
                    
                    # Create new column
                    new_column = DataColumn()
                    new_column.Name = col_name
                    new_column.DataType = tabular_data_type
                    new_column.SourceColumn = col_name
                    
                    # Set column properties
                    new_column.IsHidden = False
                    new_column.IsKey = False
                    new_column.IsUnique = False
                    
                    # Add column to table
                    new_table.Columns.Add(new_column)
                    
                except Exception as e:
                    logger.warning(f"Skipped column {row.get('column_name', 'unknown')}: {e}")
            
            # Save columns to model
            self.model.SaveChanges()
            self.refresh_table(table_name)
            return f"✅ DirectLake table '{table_name}' created successfully with {len(new_table.Columns)} columns and refresh is triggered"
                
        except Exception as e:
            logger.error(f"Failed to create DirectLake table '{table_name}': {e}")
            raise Exception(f"Failed to create DirectLake table '{table_name}': {e}")
    
    def update_column_names(self, table_name: str, old_col_name: str, new_col_name: str):
        if not self.connected: 
            logger.info("Tabular server is not connected")
            raise "Tabular server is not connected"
        try:
            table = next((t for t in self.model.Tables if t.Name == table_name), None)
            if not table:
                raise Exception(f"Table '{table_name}' not found!")
 
            column = next((c for c in table.Columns if c.Name == old_col_name), None)
            if not column:
                raise Exception(f"Column '{old_col_name}' not found!")
 
            column.Name = new_col_name
            logger.info(f"Renamed column '{old_col_name}' to '{new_col_name}'")
 
            def update_expr(expr):
                pattern = fr'{re.escape(table_name)}\[\s*{re.escape(old_col_name)}\s*\]'
                return re.sub(pattern, f"{table_name}[{new_col_name}]", expr)
 
            for tbl in self.model.Tables:
                for measure in tbl.Measures:
                    if f"{table_name}[{old_col_name}]" in measure.Expression:
                        old_expr = measure.Expression
                        measure.Expression = update_expr(measure.Expression)
                        logger.info(f"Updated measure '{measure.Name}' in table '{tbl.Name}'")
                        logger.info(f" Old: {old_expr}")
                        logger.info(f" New: {measure.Expression}")
 
            for tbl in self.model.Tables:
                for calc_col in tbl.Columns:
                    if hasattr(calc_col, 'IsCalculated') and calc_col.IsCalculated:
                        if f"{table_name}[{old_col_name}]" in calc_col.Expression:
                            old_expr = calc_col.Expression
                            calc_col.Expression = update_expr(calc_col.Expression)
                            logger.info(f"Updated calculated column '{calc_col.Name}' in table '{tbl.Name}'")
                            logger.info(f" Old: {old_expr}")
                            logger.info(f" New: {calc_col.Expression}")
 
            for rel in self.model.Relationships:
                if rel.FromTable.Name == table_name and rel.FromColumn.Name == old_col_name:
                    rel.FromColumn = table.Columns[new_col_name]
                    logger.info(f"Updated FromColumn in relationship ID {rel.ID}")
                if rel.ToTable.Name == table_name and rel.ToColumn.Name == old_col_name:
                    rel.ToColumn = table.Columns[new_col_name]
                    logger.info(f"Updated ToColumn in relationship ID {rel.ID}")
 
            self.model.RequestRefresh(RefreshType.Automatic)
            self.model.SaveChanges()
            msg = "Table name rename successfully and automatic refresh is triggered."
            logger.info("Model changes saved and refresh triggered.")
        except Exception as e: 
            msg = str(e)
        return msg
    
    def update_table_name(self, old_table_name: str, new_table_name: str, confirm: bool = False) -> str:
        if not self.connected:
            raise Exception("Tabular server is not connected")

        table = next((t for t in self.model.Tables if t.Name.lower() == old_table_name.lower()), None)
        if not table:
            raise Exception(f"❌ Table '{old_table_name}' not found!")

        if any(t.Name.lower() == new_table_name.lower() for t in self.model.Tables):
            raise Exception(f"❌ A table named '{new_table_name}' already exists.")

        # Confirm renaming
        if not confirm:
            return (
                f"⚠️ Are you sure you want to rename table '{old_table_name}' to '{new_table_name}'? "
                "Pass `confirm=True` to proceed."
            )

        # Proceed with renaming
        table.Name = new_table_name
        logger.info(f"✅ Renamed table '{old_table_name}' to '{new_table_name}'")

        def update_expr(expr):
            pattern = fr'\b{re.escape(old_table_name)}\s*\['
            return re.sub(pattern, f"{new_table_name}[", expr)

        updated_objects = []

        for tbl in self.model.Tables:
            # Update Measures
            for measure in tbl.Measures:
                if f"{old_table_name}[" in measure.Expression:
                    measure.Expression = update_expr(measure.Expression)
                    logger.info(f"🔁 Updated measure '{measure.Name}' in table '{tbl.Name}'")
                    updated_objects.append(("Measure", tbl.Name, measure.Name))

            # Update Calculated Columns
            for col in tbl.Columns:
                if hasattr(col, 'IsCalculated') and col.IsCalculated:
                    if f"{old_table_name}[" in col.Expression:
                        col.Expression = update_expr(col.Expression)
                        logger.info(f"🔁 Updated calculated column '{col.Name}' in table '{tbl.Name}'")
                        updated_objects.append(("Column", tbl.Name, col.Name))

        self.model.RequestRefresh(RefreshType.Automatic)
        self.model.SaveChanges()
        logger.info("✅ Model changes saved and refreshed.")

        return f"✅ Table '{old_table_name}' renamed to '{new_table_name}' with {len(updated_objects)} dependent objects updated."

class PowerBIMCPServer:
    def __init__(self):
        self.server = Server("MCP")
        self.sql_endpoint = SQLEndpoint()
        self.fabric = Fabric()
        self.tabular_editor = TabularEditor()
        self.is_connected = False
        self.connection_lock = threading.Lock()
        
        # Setup server handlers
        self._setup_handlers()
        
    def _setup_handlers(self):
        @self.server.list_tools()
        async def handle_list_tools() -> List[Tool]:
            return [
            Tool(
                name="initialize_sql_connection",
                description="Initialize SQL connection with server and database details.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sql_endpoint": {"type": "string"},
                        "sql_database": {"type": "string"}
                    },
                    "required": ["sql_endpoint", "sql_database"]
                }
            ),
            Tool(
                name="get_sql_tables",
                description="Retrieve a list of tables from the SQL database.",
                inputSchema={
                    "type": "object",
                    "properties": {
                    },
                    "required": []
                }
            ),
            Tool(
                name="get_sql_table_schema",
                description="Retrieve the schema of a specific table from the SQL database.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {"type": "string"}
                    },
                    "required": ["table_name"]
                }
            ),
            Tool(
                name="create_measure",
                description="Create a new measure in the model.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {"type": "string"},
                        "measure_name": {"type": "string"},
                        "dax_expression": {"type": "string"}
                    },
                    "required": ["table_name","measure_name","dax_expression"]
                }
            ),
            Tool(
                name="update_column_names",
                description="Update column names in the model.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {"type": "string"},
                        "old_col_name": {"type": "string"},
                        "new_col_name": {"type": "string"}
                    },
                    "required": ["table_name","old_col_name","new_col_name"]
                }
            ),Tool(
                name="update_table_name",
                description="Update table names in the model.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "old_table_name": {"type": "string"},
                        "new_table_name": {"type": "string"},
                        "confirm": {"type": "boolean"}
                    },
                    "required": ["old_table_name","new_table_name"]
                }
            ),
            Tool(
                name="execute_sql_query",
                description="Execute a SQL query against the database.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"}
                    },
                    "required": ["query"]
                }
            ),
            Tool(
                name="execute_dax_query",
                description="Execute a DAX query against the database.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "dax_query": {"type": "string"}
                    },
                    "required": ["dax_query"]
                }
            ),
            Tool(
                name="get_workspace_info",
                description="Retrieve information about the workspace.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace_identifier": {"type": "string"}
                    },
                    "required": ["workspace_identifier"]
                }
            ),
            Tool(
                name="get_lakehouse_info",
                description="Retrieve information about the lakehouse and sql endpoint.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace_identifier": {"type": "string"},
                        "lakehouse_identifier": {"type": "string"}
                    },
                    "required": ["workspace_identifier", "lakehouse_identifier"]
                }
            ),
            Tool(
                name="create_lakehouse",
                description="Create a new lakehouse.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace_identifier": {"type": "string"},
                        "lakehouse_name": {"type": "string"}
                    },
                    "required": ["workspace_identifier", "lakehouse_name"]
                }
            ),
            Tool(
                name="create_lakehouse_shortcut",
                description="Create a lakehouse shortcut with MCP elicitation for approval.",
                inputSchema={
                    "type": "object",
                    "properties": {
                    },
                    "required": []
                }
            ),
            Tool(
                name="connect_dataset",
                description="Connect to a Power BI dataset using workspace_identifier (workspace name or ID) and database_name (dataset name)",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace_identifier": {
                            "type": "string",
                            "description": "The workspace name or workspace ID (not server_name)"
                        },
                        "database_name": {
                            "type": "string",
                            "description": "The Power BI dataset/database name"
                        }
                    },
                    "required": ["workspace_identifier","database_name"]
                }
            ),
            Tool(
                name="add_directlake_table",
                description="Add a DirectLake table to the Power BI dataset using workspace_identifier (workspace name or ID) and database_name (dataset name)",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_table": {
                            "type": "string",
                            "description": "The source table name"
                        },
                        "table_name": {
                            "type": "string",
                            "description": "power bi table name"
                        }
                    },
                    "required": ["source_table"]
                }
            ),
            Tool(
                name="disconnect_dataset",
                description="Disconnecting power bi dataset after use",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            ),
            Tool(
                name="list_tables",
                description="List all tables in the connected semantic model.",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            ),
            Tool(
                name="list_all_relationships",
                description="List all relationships in the connected semantic model.",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            ),
            Tool(
                name="create_semantic_model",
                description="Create a comprehensive DirectLake semantic model using TMSL for full DAX Studio and XMLA support with automatic refresh",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace_identifier": {"type": "string", "description": "The workspace name or workspace ID"},
                        "lakehouse_identifier": {"type": "string", "description": "The lakehouse name or lakehouse ID"},
                        "semantic_model_name": {"type": "string", "description": "Name for the new semantic model"},
                        "selected_tables": {"type": "array", "items": {"type": "string"}, "description": "Optional list of specific tables to include"},
                        "description": {"type": "string", "description": "Optional description for the semantic model"}
                    },
                    "required": ["workspace_identifier", "semantic_model_name", "lakehouse_identifier"]
                }
            ),
            Tool(
                name="select_tables_with_schema",
                description="Select specific tables and return their schemas, or return all tables if none specified",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "selected_table_names": {"type": "array", "items": {"type": "string"}, "description": "Optional list of specific table names to get schema for"}
                    },
                    "required": []
                }
            ),
            Tool(
                name="refresh_semantic_model",
                description="Refresh a Power BI dataset using workspace_identifier (workspace name or ID) and database_name (dataset name)",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace_identifier": {
                            "type": "string",
                            "description": "The workspace name or workspace ID (not server_name)"
                        },
                        "semantic_model_name": {
                            "type": "string",
                            "description": "The name of the semantic model to refresh"
                        },
                        "refresh_type": {
                            "type": "string",
                            "description": "The type of refresh to perform"
                        }
                    },
                    "required": ["workspace_identifier","semantic_model_name"]
                }
            )
        ]
        @self.server.call_tool()
        async def handle_call_tool(name: str, arguments: Optional[Dict[str, Any]]) -> List[TextContent]:
            """Handle tool calls and return results as TextContent"""
            try:
                logger.info(f"Handling tool call: {name}")
                
                if name == "initialize_sql_connection":
                    result = await self._handle_initialize_sql_connection(arguments)
                elif name == "get_sql_tables":
                    result = await self._handle_get_sql_tables(arguments)
                elif name == "get_sql_table_schema":
                    result = await self._handle_get_sql_table_schema(arguments)
                elif name == "execute_sql_query":
                    result = await self._handle_execute_sql_query(arguments)
                elif name == "get_workspace_info":
                    result = await self._handle_get_workspace_info(arguments)
                elif name == "get_lakehouse_info":
                    result = await self._handle_get_lakehouse_info(arguments)
                elif name == "create_lakehouse":
                    result = await self._handle_create_lakehouse(arguments)
                elif name == "create_lakehouse_shortcut":
                    result = await self._handle_create_lakehouse_shortcut(arguments)
                elif name == "connect_dataset":
                    result = await self._handle_connect_dataset(arguments)
                elif name == "list_tables":
                    result = await self._handle_list_tables(arguments)
                elif name == "disconnect_dataset":
                    result = await self._handle_disconnect_dataset(arguments)
                elif name == "create_semantic_model":
                    result = await self._handle_create_semantic_model(arguments)
                elif name == "select_tables_with_schema":
                    result = await self._handle_select_tables_with_schema(arguments)
                elif name == "refresh_semantic_model":
                    result = await self._handle_refresh_semantic_model(arguments)
                elif name == "execute_dax_query":
                    result = await self._handle_execute_dax_query(arguments)
                elif name == "create_measure":
                    result = await self._handle_create_measure(arguments)
                elif name == "list_all_relationships":
                    result = await self._handle_list_all_relationships(arguments)  
                elif name == "update_column_names":
                    result = await self._handle_update_column_names(arguments)
                elif name == "update_table_name":
                    result = await self._handle_update_table_name(arguments)
                else:
                    logger.warning(f"Unknown tool: {name}")
                    return [TextContent(type="text", text=f"Unknown tool: {name}")]
                
                # Convert string result to TextContent
                return [TextContent(type="text", text=result)]
                
            except Exception as e:
                logger.error(f"Error executing {name}: {str(e)}", exc_info=True)
                return [TextContent(type="text", text=f"Error executing {name}: {str(e)}")]

    async def _handle_initialize_sql_connection(self, arguments: Dict[str, Any]) -> str:
        """Handle initialization of SQL connection"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.sql_endpoint.initialize_sql_connection,
                    arguments["sql_endpoint"],
                    arguments["sql_database"]
                )
                return str(result)

        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"

    async def _handle_get_sql_tables(self, arguments: Dict[str, Any]) -> str:
        """Handle retrieval of SQL tables"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.sql_endpoint.get_sql_tables
                )
                return str(result)

        except Exception as e:
            return f"Connection failed: {str(e)}"

    async def _handle_get_sql_table_schema(self, arguments: Dict[str, Any]) -> str:
        """Handle retrieval of SQL table schema"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.sql_endpoint.get_sql_table_schema,
                    arguments["table_name"]
                )
                return str(result)

        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"
        
    async def _handle_create_measure(self, arguments: Dict[str, Any]) -> str:
        """Handle creation of a new measure"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.create_measure,
                    arguments["table_name"],
                    arguments["measure_name"],
                    arguments["dax_expression"]
                )
                return str(result)

        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"


    async def _handle_execute_sql_query(self, arguments: Dict[str, Any]) -> str:
        """Handle execution of SQL query"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.sql_endpoint.execute_sql_query,
                    arguments["query"]
                )
                return str(result)

        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"
    
    async def _handle_execute_dax_query(self, arguments: Dict[str, Any]) -> str:
        """Handle execution of DAX query"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.execute_dax_query,
                    arguments["dax_query"]
                )
                return str(result)

        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"

    async def _handle_get_workspace_info(self, arguments: Dict[str, Any]) -> str:
        """Handle retrieval of workspace information"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.fabric.get_workspace_info,
                    arguments["workspace_identifier"]
                )
                return str(result)

        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"
    
    async def _handle_get_lakehouse_info(self, arguments: Dict[str, Any]) -> str:
        """Handle retrieval of lakehouse information"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.fabric.get_lakehouse_info,
                    arguments["workspace_identifier"],
                    arguments["lakehouse_identifier"]
                )
                return str(result)

        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"

    async def _handle_create_lakehouse(self, arguments: Dict[str, Any]) -> str:
        """Handle creation of lakehouse"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.fabric.create_lakehouse,
                    arguments["workspace_identifier"],
                    arguments["lakehouse_name"]
                )
                return str(result)

        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"

    
    async def _handle_connect_dataset(self, arguments: Dict[str, Any]) -> str:
        """Handle connection to Power BI"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.connect_dataset,
                    arguments["workspace_identifier"],
                    arguments["database_name"]
                )
                return str(result)
                
        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"

    async def _handle_refresh_semantic_model(self, arguments: Dict[str, Any]) -> str:
        """Handle refresh of Power BI dataset"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.refresh_semantic_model,
                    arguments["workspace_identifier"],
                    arguments["semantic_model_name"],
                    arguments["refresh_type"]
                )
                return str(result)
                
        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"

    async def _handle_update_table_name(self, arguments: Dict[str, Any]) -> str:
        """Handle update of table name in Power BI dataset"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.refresh_semantic_model,
                    arguments["old_table_name"],
                    arguments["new_table_name"],
                    arguments["confirm"]
                )
                return str(result)
                
        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"

    async def _handle_update_column_names(self, arguments: Dict[str, Any]) -> str:
        """Handle update of column names in Power BI dataset"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.refresh_semantic_model,
                    arguments["table_name"],
                    arguments["new_col_name"],
                    arguments["old_col_name"]
                )
                return str(result)
                
        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"
    
    async def _handle_disconnect_dataset(self, arguments: Dict[str, Any]) -> str:
        """Handle disconnection from Power BI dataset"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                msg = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.disconnect_dataset
                )
                return str(msg)

        except Exception as e:
            logger.error(f"error in disconnecting the model from mcp server: {str(e)}")
            return f"error in disconnecting the model from mcp server: {str(e)}"

    async def _handle_list_tables(self, arguments: Dict[str, Any]) -> str:
        """Handle listing tables in the Power BI dataset"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                msg = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.list_tables
                )
                return str(msg)

        except Exception as e:
            logger.error(f"Error listing tables in the model from mcp server: {str(e)}")
            return f"Error listing tables in the model from mcp server: {str(e)}"

    async def _handle_list_all_relationships(self, arguments: Dict[str, Any]) -> str:
        """Handle listing all relationships in the Power BI dataset"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                msg = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.list_all_relationships
                )
                return str(msg)

        except Exception as e:
            logger.error(f"Error listing all relationships in the model from mcp server: {str(e)}")
            return f"Error listing all relationships in the model from mcp server: {str(e)}"

    async def _handle_create_semantic_model(self, arguments: Dict[str, Any]) -> str:
        """Handle creation of semantic model"""
        print("🔥 HANDLER CALLED - _handle_create_semantic_model")
        logger.info("🔥 HANDLER CALLED - _handle_create_semantic_model")
        try:
            print(f"🔥 Handler received arguments: {arguments}")
            logger.info(f"🔥 Handler received arguments: {arguments}")
            selected_tables = arguments.get("selected_tables")
            print(f"🔥 Extracted selected_tables: {selected_tables}, type: {type(selected_tables)}")
            logger.info(f"🔥 Extracted selected_tables: {selected_tables}, type: {type(selected_tables)}")
            
            with self.connection_lock:
                print("🔥 About to call tabular_editor.create_semantic_model")
                logger.info("🔥 About to call tabular_editor.create_semantic_model")
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.create_semantic_model,
                    arguments["workspace_identifier"],
                    arguments["lakehouse_identifier"],
                    arguments["semantic_model_name"],
                    selected_tables,
                    arguments.get("description")
                )
                print(f"🔥 Result from create_semantic_model: {result}")
                logger.info(f"🔥 Result from create_semantic_model: {result}")
                return json.dumps(result)

        except Exception as e:
            print(f"🔥 ERROR in handler: {str(e)}")
            logger.error(f"🔥 ERROR in handler: {str(e)}")
            return f"Error creating semantic model: {str(e)}"

    async def _handle_select_tables_with_schema(self, arguments: Dict[str, Any]) -> str:
        """Handle selection of tables with schema"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.select_tables_with_schema,
                    arguments.get("selected_table_names")
                )
                return json.dumps(result)

        except Exception as e:
            logger.error(f"Error selecting tables with schema: {str(e)}")
            return f"Error selecting tables with schema: {str(e)}"

    async def _handle_create_lakehouse_shortcut(self, arguments: Dict[str, Any]) -> str:
        """Handle creation of lakehouse shortcut with approval elicitation"""
        try:
            with self.connection_lock:
                # Call the unified method with all parameters including approved, using get to handle optional parameters
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: asyncio.run(self.fabric.create_lakehouse_shortcut(
                        arguments.get("target_workspace"),
                        arguments.get("target_lakehouse"),
                        arguments.get("target_shortcut_path"),
                        arguments.get("target_shortcut_name"),
                        arguments.get("source_workspace"),
                        arguments.get("source_lakehouse"),
                        arguments.get("source_path"),
                        arguments.get("approved", False)
                    ))
                )
                
                return json.dumps(result)

        except Exception as e:
            logger.error(f"Error creating shortcut: {str(e)}")
            return f"Error creating shortcut: {str(e)}"
    
    async def run(self):
        """Run the MCP server"""
        try:
            logger.info("Starting Power BI MCP Server...")
            async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
                await self.server.run(
                    read_stream,
                    write_stream,
                    InitializationOptions(
                        server_name="MCP",
                        server_version="1.0.1",
                        capabilities=self.server.get_capabilities(
                            notification_options=NotificationOptions(),
                            experimental_capabilities={},
                        ),
                    ),
                )
        except anyio.BrokenResourceError:
            logger.info("Client disconnected")
        except Exception as e:
            logger.error(f"Server error: {e}", exc_info=True)
        finally:
            logger.info("Server shutting down")

# Main entry point
async def main():
    server = PowerBIMCPServer()
    await server.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


