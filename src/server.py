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

from Microsoft.AnalysisServices.Tabular import Server as TabularServer, Table as TabularTable, EntityPartitionSource, RefreshType, DataType, DataColumn, Partition as TabularPartition, ModeType, ModelRole, TablePermission, MetadataPermission,SingleColumnRelationship,ModelPermission, CrossFilteringBehavior,CalculatedPartitionSource,Measure, Annotation # type: ignore
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

    async def create_lakehouse_shortcut(self, target_workspace: str = None, target_lakehouse: str = None, target_shortcut_path: str = None, target_shortcut_name: str = None, source_workspace: str = None, source_lakehouse: str = None, source_path: str = None) -> dict:
        """Creating shortcuts from authoritative workspace and lakehouse into target workspace, target lakehouse and target path without approval requirement."""
        try:
            # If no parameters provided, return elicitation prompt to collect all details
            if not all([target_workspace, target_lakehouse, target_shortcut_path, target_shortcut_name, source_workspace, source_lakehouse, source_path]):
                collection_prompt = """
Create Lakehouse Shortcut

Please provide the following information for creating the lakehouse shortcut:

1. Target Workspace: Name or ID of the target workspace
2. Target Lakehouse: Name or ID of the target lakehouse  
3. Target Shortcut Path: Path in target lakehouse (e.g., 'Tables' or 'Files/folder')
4. Target Shortcut Name: Name for the shortcut
5. Source Workspace: Name or ID of the source workspace
6. Source Lakehouse: Name or ID of the source lakehouse
7. Source Path: Path in source lakehouse (e.g., 'Tables/table_name' or 'Files/folder')

The shortcut will be created automatically once all details are provided.
                """

                return {
                    "type": "elicitation_required",
                    "prompt": collection_prompt.strip(),
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

            # Proceed directly with shortcut creation (no approval required)
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

            import time
            time.sleep(10)  # Give model time to be available
            server.Refresh()
        
            
            # Check if our model exists and automatically refresh it
            created_model = server.Databases.Find(semantic_model_name)
            if created_model:
                logger.info(f"Successfully verified model '{semantic_model_name}' was created")
            self.model = created_model.Model
            self.connected = True
            logger.info("Disconnected from Analysis Services")
            
            # Prepare the result
            creation_result = {
                "success": True, 
                "workspace_id": workspace_id,
                "workspace_name": workspace_name,
                "model_name": semantic_model_name,
                "tables_added": [table['name'] for table in tmsl_tables],
                "total_tables": len(tmsl_tables)
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
        
    def create_table_security_role(self, role_name: str, table_name: str, filter_expression: str) -> str:
        """Create table security role with RLS filter expression."""
        if not self.connected:
            raise Exception("Tabular server is not connected")
        try:
            # Create or get the role
            existing_role = next((r for r in self.model.Roles if r.Name.lower() == role_name.lower()), None)
            if not existing_role:
                # Create new role if it doesn't exist
                new_role = ModelRole()
                new_role.Name = role_name
                new_role.Description = f"Security role for {table_name} table"
                # Ensure the role has proper permissions for Power BI RLS
                new_role.ModelPermission = ModelPermission.Read  # Add this line
                self.model.Roles.Add(new_role)
                role = new_role
                logger.info(f"Created new role '{role_name}'")
            else:
                role = existing_role
                logger.info(f"Using existing role '{role_name}'")
            
            # Find the table
            table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
            if not table:
                raise Exception(f"Table '{table_name}' not found in the model.")
            
            # Check if table permission already exists
            existing_permission = next((tp for tp in role.TablePermissions 
                                      if tp.Table.Name.lower() == table_name.lower()), None)
            if existing_permission:
                # Update existing permission
                existing_permission.FilterExpression = filter_expression
                existing_permission.MetadataPermission = MetadataPermission.Read
                logger.info(f"Updated existing table permission for '{table_name}'")
            else:
                # Create new table permission
                table_permission = TablePermission()
                table_permission.Table = table
                table_permission.MetadataPermission = MetadataPermission.Read
                table_permission.FilterExpression = filter_expression
                
                # Add permission to role
                role.TablePermissions.Add(table_permission)
                logger.info(f"Created new table permission for '{table_name}'")
            
            # Save changes
            self.model.SaveChanges()
            
            success_msg = f"✅ Table security role '{role_name}' created/updated for table '{table_name}' with RLS filter"
            logger.info(success_msg)
            return success_msg
            
        except Exception as e:
            logger.error(f"Failed to create table security role: {e}")
            raise Exception(f"Failed to create table security role: {e}")
    
    def update_table_security_role(self, role_name: str, table_name: str = None, 
                              new_filter_expression: str = None, new_role_name: str = None,
                              confirm: bool = False) -> str:
        """Update an existing table security role."""
        if not self.connected:
            raise Exception("Tabular server is not connected")
        try:
            # Find the role
            role = next((r for r in self.model.Roles if r.Name.lower() == role_name.lower()), None)
            if not role:
                raise Exception(f"Role '{role_name}' not found in the model.")
            
            # If table_name is specified, update specific table permission
            if table_name:
                table_permission = next((tp for tp in role.TablePermissions 
                                       if tp.Table.Name.lower() == table_name.lower()), None)
                if not table_permission:
                    raise Exception(f"Table permission for '{table_name}' not found in role '{role_name}'.")
                
                # Prepare update preview
                preview = []
                if new_filter_expression and new_filter_expression != table_permission.FilterExpression:
                    preview.append(f"Filter: '{table_permission.FilterExpression}' → '{new_filter_expression}'")
                
                if not preview and not new_role_name:
                    return f"No changes to update for role '{role_name}' on table '{table_name}'."
                
                # Confirm update
                if not confirm:
                    preview_text = "\n".join(preview) if preview else "No table-specific changes"
                    return (
                        f"⚠️ Are you sure you want to update role '{role_name}' for table '{table_name}'?\n" +
                        preview_text +
                        (f"\nRole rename: '{role_name}' → '{new_role_name}'" if new_role_name else "") +
                        "\nPass `confirm=True` to proceed."
                    )
                
                # Perform table permission update
                if new_filter_expression:
                    table_permission.FilterExpression = new_filter_expression
                    logger.info(f"Updated filter expression for table '{table_name}' in role '{role_name}'")
            
            # Update role name if specified
            if new_role_name and new_role_name != role_name:
                if any(r.Name.lower() == new_role_name.lower() for r in self.model.Roles):
                    raise Exception(f"A role named '{new_role_name}' already exists.")
                role.Name = new_role_name
                logger.info(f"Renamed role '{role_name}' to '{new_role_name}'")
            
            # Save changes
            self.model.SaveChanges()
            
            return f"✅ Table security role updated successfully."
            
        except Exception as e:
            logger.error(f"Failed to update table security role: {e}")
            raise Exception(f"Failed to update table security role: {e}")
        
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
            # Create a new Measure object
            new_measure = Measure()
            new_measure.Name = measure_name
            new_measure.Expression = dax_expression
            new_measure.Description = ""
            
            # Add the measure to the table's Measures collection
            table.Measures.Add(new_measure)
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
    
    # REMOVED: update_column_names - use safe_rename_with_dependencies instead
    # This function was redundant as safe_rename_with_dependencies provides
    # comprehensive dependency checking and safe renaming for columns
    
    # REMOVED: update_table_name - use safe_rename_with_dependencies instead
    # This function was redundant as safe_rename_with_dependencies provides
    # comprehensive dependency checking and safe renaming for tables
    
    def create_relationship(self, from_table: str, from_column: str, to_table: str, to_column: str, 
                       is_active: bool = True, cross_filter_direction: str = "OneDirection") -> str:
        """Create a relationship between two tables in the tabular model."""
        if not self.connected:
            raise Exception("Tabular server is not connected. Try Connecting to SQL endpoint and Database.")
        try:
            # Find the tables
            from_table_obj = next((t for t in self.model.Tables if t.Name.lower() == from_table.lower()), None)
            if not from_table_obj:
                raise Exception(f"From table '{from_table}' not found in the model.")
            
            to_table_obj = next((t for t in self.model.Tables if t.Name.lower() == to_table.lower()), None)
            if not to_table_obj:
                raise Exception(f"To table '{to_table}' not found in the model.")
            
            # Find the columns
            from_column_obj = next((c for c in from_table_obj.Columns if c.Name.lower() == from_column.lower()), None)
            if not from_column_obj:
                raise Exception(f"Column '{from_column}' not found in table '{from_table}'.")
            
            to_column_obj = next((c for c in to_table_obj.Columns if c.Name.lower() == to_column.lower()), None)
            if not to_column_obj:
                raise Exception(f"Column '{to_column}' not found in table '{to_table}'.")
            
            # Check if relationship already exists
            existing_rel = next((rel for rel in self.model.Relationships 
                               if rel.FromTable.Name.lower() == from_table.lower() and 
                                  rel.FromColumn.Name.lower() == from_column.lower() and
                                  rel.ToTable.Name.lower() == to_table.lower() and
                                  rel.ToColumn.Name.lower() == to_column.lower()), None)
            
            if existing_rel:
                raise Exception(f"Relationship already exists between {from_table}[{from_column}] and {to_table}[{to_column}]")
            
            # Create new relationship
            new_relationship = SingleColumnRelationship()
            new_relationship.Name = f"{from_table}_{from_column}_to_{to_table}_{to_column}"
            new_relationship.FromTable = from_table_obj
            new_relationship.FromColumn = from_column_obj
            new_relationship.ToTable = to_table_obj
            new_relationship.ToColumn = to_column_obj
            new_relationship.IsActive = is_active
            
            # Set cross filter direction
            if cross_filter_direction.lower() == "onedirection":
                new_relationship.CrossFilteringBehavior = CrossFilteringBehavior.OneDirection
            elif cross_filter_direction.lower() == "bothdirections":
                new_relationship.CrossFilteringBehavior = CrossFilteringBehavior.BothDirections
            elif cross_filter_direction.lower() == "automatic":
                new_relationship.CrossFilteringBehavior = CrossFilteringBehavior.Automatic
            else:
                new_relationship.CrossFilteringBehavior = CrossFilteringBehavior.OneDirection
            
            # Add relationship to model
            self.model.Relationships.Add(new_relationship)
            self.model.SaveChanges()
            
            success_msg = f"✅ Relationship created: {from_table}[{from_column}] -> {to_table}[{to_column}]"
            logger.info(success_msg)
            return success_msg
            
        except Exception as e:
            logger.error(f"Failed to create relationship: {e}")
            raise Exception(f"Failed to create relationship: {e}")
    
    def check_date_table_exists(self, table_name: str = None) -> Dict[str, Any]:
        """Check if a date table exists in the model and return its details."""
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        
        try:
            date_tables_info = []
            current_date_table = None
            
            # If table_name is specified, check that specific table
            if table_name:
                table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
                if not table:
                    raise Exception(f"Table '{table_name}' not found in the model.")
                
                # Check if it's marked as a date table
                is_date_table = hasattr(table, 'DataCategory') and table.DataCategory == 'Time'
                
                # Look for date columns
                date_columns = []
                for column in table.Columns:
                    if hasattr(column, 'DataType') and column.DataType == DataType.DateTime:
                        date_columns.append({
                            "name": column.Name,
                            "is_key": hasattr(column, 'IsKey') and column.IsKey,
                            "is_hidden": hasattr(column, 'IsHidden') and column.IsHidden
                        })
                
                date_tables_info.append({
                    "table_name": table.Name,
                    "is_date_table": is_date_table,
                    "date_columns": date_columns,
                    "column_count": len(table.Columns)
                })
                
                if is_date_table:
                    current_date_table = table.Name
            else:
                # Check all tables for potential date tables
                for table in self.model.Tables:
                    # Check if it's marked as a date table
                    is_date_table = hasattr(table, 'DataCategory') and table.DataCategory == 'Time'
                    
                    # Look for date columns
                    date_columns = []
                    for column in table.Columns:
                        if hasattr(column, 'DataType') and column.DataType == DataType.DateTime:
                            date_columns.append({
                                "name": column.Name,
                                "is_key": hasattr(column, 'IsKey') and column.IsKey,
                                "is_hidden": hasattr(column, 'IsHidden') and column.IsHidden
                            })
                    
                    # Consider it a potential date table if it has date columns or is marked as Time category
                    if is_date_table or len(date_columns) > 0:
                        date_tables_info.append({
                            "table_name": table.Name,
                            "is_date_table": is_date_table,
                            "date_columns": date_columns,
                            "column_count": len(table.Columns)
                        })
                        
                        if is_date_table:
                            current_date_table = table.Name
            
            result = {
                "current_date_table": current_date_table,
                "potential_date_tables": date_tables_info,
                "total_tables_checked": len(self.model.Tables) if not table_name else 1,
                "has_date_table": current_date_table is not None
            }
            
            logger.info(f"Date table check completed. Found {len(date_tables_info)} potential date tables.")
            return result
            
        except Exception as e:
            logger.error(f"Failed to check date table: {e}")
            raise Exception(f"Failed to check date table: {e}")
    
    def mark_as_date_table(self, table_name: str, date_column: str = None) -> str:
        """Mark a table as a date table in the model."""
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        
        try:
            # Find the table
            table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
            if not table:
                raise Exception(f"Table '{table_name}' not found in the model.")
            
            # Check if there's already a date table in the model
            existing_date_table = None
            for t in self.model.Tables:
                if hasattr(t, 'DataCategory') and t.DataCategory == 'Time':
                    existing_date_table = t.Name
                    break
            
            if existing_date_table and existing_date_table.lower() != table_name.lower():
                logger.warning(f"Another table '{existing_date_table}' is already marked as date table. This will replace it.")
            
            # Find date columns in the table
            date_columns = []
            for column in table.Columns:
                if hasattr(column, 'DataType') and column.DataType == DataType.DateTime:
                    date_columns.append(column)
            
            if not date_columns:
                raise Exception(f"Table '{table_name}' has no date/datetime columns. Cannot mark as date table.")
            
            # If date_column is specified, validate it exists
            key_column = None
            if date_column:
                key_column = next((c for c in date_columns if c.Name.lower() == date_column.lower()), None)
                if not key_column:
                    available_columns = [c.Name for c in date_columns]
                    raise Exception(f"Date column '{date_column}' not found. Available date columns: {available_columns}")
            else:
                # Use the first date column as key
                key_column = date_columns[0]
            
            # Mark the table as a date table
            table.DataCategory = 'Time'
            
            # Set the key column
            if key_column:
                # First, remove IsKey from all user columns in the table (skip system columns)
                for column in table.Columns:
                    # Skip system-generated columns (like RowNumber columns)
                    if (hasattr(column, 'IsKey') and 
                        'RowNumber' not in column.Name and
                        not column.Name.startswith('RowNumber')):
                        column.IsKey = False
                
                # Set the specified date column as key
                key_column.IsKey = True
                logger.info(f"Set '{key_column.Name}' as key column for date table.")
            
            # Save changes
            self.model.SaveChanges()
            
            success_msg = f"✅ Table '{table_name}' successfully marked as date table with key column '{key_column.Name if key_column else 'None'}'"
            logger.info(success_msg)
            return success_msg
            
        except Exception as e:
            logger.error(f"Failed to mark table as date table: {e}")
            raise Exception(f"Failed to mark table as date table: {e}")
    
    def unmark_date_table(self, table_name: str) -> str:
        """Remove date table marking from a table."""
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        
        try:
            # Find the table
            table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
            if not table:
                raise Exception(f"Table '{table_name}' not found in the model.")
            
            # Check if it's currently a date table
            is_date_table = hasattr(table, 'DataCategory') and table.DataCategory == 'Time'
            if not is_date_table:
                return f"Table '{table_name}' is not currently marked as a date table."
            
            # Remove date table marking
            table.DataCategory = None
            
            # Remove key designation from date columns (skip system columns)
            for column in table.Columns:
                if (hasattr(column, 'IsKey') and column.IsKey and 
                    hasattr(column, 'DataType') and column.DataType == DataType.DateTime and
                    'RowNumber' not in column.Name and not column.Name.startswith('RowNumber')):
                    column.IsKey = False
                    logger.info(f"Removed key designation from column '{column.Name}'")
            
            # Save changes
            self.model.SaveChanges()
            
            success_msg = f"✅ Date table marking removed from table '{table_name}'"
            logger.info(success_msg)
            return success_msg
            
        except Exception as e:
            logger.error(f"Failed to unmark date table: {e}")
            raise Exception(f"Failed to unmark date table: {e}")
    
    def get_column_properties(self, table_name: str, column_name: str) -> Dict[str, Any]:
        """
        Get all available properties for a specific column with their current values and metadata.
        
        Args:
            table_name: Name of the table containing the column
            column_name: Name of the column to inspect
            
        Returns:
            Dictionary with property details including current values, types, and descriptions
        """
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        
        try:
            # Find the table
            table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
            if not table:
                raise Exception(f"Table '{table_name}' not found in the model.")
            
            # Find the column
            column = next((c for c in table.Columns if c.Name.lower() == column_name.lower()), None)
            if not column:
                raise Exception(f"Column '{column_name}' not found in table '{table_name}'.")
            
            # Define all possible column properties with their metadata
            property_definitions = {
                # Core Properties
                'Name': {'type': 'string', 'description': 'The name of the column', 'editable': True},
                'Description': {'type': 'string', 'description': 'Description of the column', 'editable': True},
                'DataType': {'type': 'DataType', 'description': 'Data type (String, Int64, DateTime, etc.)', 'editable': True},
                'SourceColumn': {'type': 'string', 'description': 'Source column name from data source', 'editable': True},
                
                # Visibility and Behavior
                'IsHidden': {'type': 'boolean', 'description': 'Whether column is hidden from client tools', 'editable': True},
                'IsKey': {'type': 'boolean', 'description': 'Whether column is marked as a key column', 'editable': True},
                'IsUnique': {'type': 'boolean', 'description': 'Whether column contains unique values', 'editable': True},
                'IsAvailableInMdx': {'type': 'boolean', 'description': 'Whether available in MDX queries', 'editable': True},
                'IsNullable': {'type': 'boolean', 'description': 'Whether column can contain null values', 'editable': True},
                
                # Calculated Columns
                'Expression': {'type': 'string', 'description': 'DAX expression for calculated columns', 'editable': True},
                'IsCalculated': {'type': 'boolean', 'description': 'Whether this is a calculated column', 'editable': False},
                
                # Formatting and Display
                'FormatString': {'type': 'string', 'description': 'Format string for display (e.g., "#,0", "mm/dd/yyyy")', 'editable': True},
                'DisplayFolder': {'type': 'string', 'description': 'Display folder in client tools', 'editable': True},
                'SortByColumn': {'type': 'Column', 'description': 'Reference to column to sort by', 'editable': True},
                'DisplayOrdinal': {'type': 'integer', 'description': 'Display order in client tools', 'editable': True},
                
                # Data Category and Summarization
                'DataCategory': {'type': 'string', 'description': 'Data category (e.g., "Time", "Geography")', 'editable': True},
                'SummarizeBy': {'type': 'AggregateFunction', 'description': 'Default aggregation function', 'editable': True},
                'IsDefaultImage': {'type': 'boolean', 'description': 'Whether this is the default image column', 'editable': True},
                'IsDefaultLabel': {'type': 'boolean', 'description': 'Whether this is the default label column', 'editable': True},
                
                # Encoding and Storage
                'EncodingHint': {'type': 'EncodingHintType', 'description': 'Storage encoding hint', 'editable': True},
                'State': {'type': 'ObjectState', 'description': 'Current state of the column', 'editable': False},
                
                # Row Level Security
                'IsPrivate': {'type': 'boolean', 'description': 'Whether column is private (RLS)', 'editable': True},
                
                # Lineage and Dependencies
                'ModifiedTime': {'type': 'DateTime', 'description': 'Last modification time', 'editable': False},
                'RefreshedTime': {'type': 'DateTime', 'description': 'Last refresh time', 'editable': False},
                'StructureModifiedTime': {'type': 'DateTime', 'description': 'Structure modification time', 'editable': False},
                
                # Annotations and Extended Properties
                'Annotations': {'type': 'AnnotationCollection', 'description': 'Custom metadata annotations', 'editable': True},
                'ExtendedProperties': {'type': 'ExtendedPropertyCollection', 'description': 'Extended properties for Power BI', 'editable': True},
                
                # Variations (Advanced)
                'Variations': {'type': 'VariationCollection', 'description': 'Column variations for different contexts', 'editable': True}
            }
            
            result = {
                'column_info': {
                    'table_name': table_name,
                    'column_name': column_name,
                    'column_type': str(type(column).__name__)
                },
                'available_properties': {},
                'current_values': {},
                'editable_properties': [],
                'readonly_properties': []
            }
            
            # Check each property and get its current value
            for prop_name, prop_info in property_definitions.items():
                try:
                    if hasattr(column, prop_name):
                        current_value = getattr(column, prop_name)
                        
                        # Convert complex objects to string representation
                        if current_value is not None:
                            if hasattr(current_value, 'Name'):  # For referenced objects
                                display_value = f"Reference: {current_value.Name}"
                            elif hasattr(current_value, '__str__') and not isinstance(current_value, (str, int, float, bool)):
                                display_value = str(current_value)
                            else:
                                display_value = current_value
                        else:
                            display_value = None
                        
                        result['available_properties'][prop_name] = prop_info
                        result['current_values'][prop_name] = display_value
                        
                        if prop_info['editable']:
                            result['editable_properties'].append(prop_name)
                        else:
                            result['readonly_properties'].append(prop_name)
                    else:
                        result['available_properties'][prop_name] = {
                            **prop_info,
                            'status': 'Not available in this version'
                        }
                except Exception as e:
                    result['available_properties'][prop_name] = {
                        **prop_info,
                        'status': f'Error accessing: {str(e)}'
                    }
            
            logger.info(f"Retrieved {len(result['available_properties'])} properties for column '{column_name}' in table '{table_name}'")
            return result
            
        except Exception as e:
            logger.error(f"Failed to get column properties: {e}")
            raise Exception(f"Failed to get column properties: {e}")
    
    def get_measure_properties(self, table_name: str, measure_name: str) -> Dict[str, Any]:
        """
        Get all available properties for a specific measure with their current values and metadata.
        
        Args:
            table_name: Name of the table containing the measure
            measure_name: Name of the measure to inspect
            
        Returns:
            Dictionary with property details including current values, types, and descriptions
        """
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        
        try:
            # Find the table
            table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
            if not table:
                raise Exception(f"Table '{table_name}' not found in the model.")
            
            # Find the measure
            measure = next((m for m in table.Measures if m.Name.lower() == measure_name.lower()), None)
            if not measure:
                raise Exception(f"Measure '{measure_name}' not found in table '{table_name}'.")
            
            # Define all possible measure properties with their metadata
            property_definitions = {
                # Core Properties
                'Name': {'type': 'string', 'description': 'The name of the measure', 'editable': True},
                'Description': {'type': 'string', 'description': 'Description of the measure', 'editable': True},
                'Expression': {'type': 'string', 'description': 'DAX expression for the measure', 'editable': True},
                
                # Visibility and Behavior
                'IsHidden': {'type': 'boolean', 'description': 'Whether measure is hidden from client tools', 'editable': True},
                'IsSimpleMeasure': {'type': 'boolean', 'description': 'Whether this is a simple measure', 'editable': False},
                
                # Formatting and Display
                'FormatString': {'type': 'string', 'description': 'Format string for display (e.g., "#,0.00", "0.0%")', 'editable': True},
                'DisplayFolder': {'type': 'string', 'description': 'Display folder in client tools', 'editable': True},
                'DisplayOrdinal': {'type': 'integer', 'description': 'Display order in client tools', 'editable': True},
                
                # KPI Properties
                'KPI': {'type': 'KPI', 'description': 'Associated KPI object if this measure is a KPI', 'editable': True},
                
                # Data Category
                'DataCategory': {'type': 'string', 'description': 'Data category for the measure', 'editable': True},
                
                # State and Metadata
                'State': {'type': 'ObjectState', 'description': 'Current state of the measure', 'editable': False},
                'ModifiedTime': {'type': 'DateTime', 'description': 'Last modification time', 'editable': False},
                'RefreshedTime': {'type': 'DateTime', 'description': 'Last refresh time', 'editable': False},
                'StructureModifiedTime': {'type': 'DateTime', 'description': 'Structure modification time', 'editable': False},
                
                # Lineage Information
                'LineageTag': {'type': 'string', 'description': 'Unique lineage identifier', 'editable': True},
                'SourceLineageTag': {'type': 'string', 'description': 'Source lineage identifier', 'editable': True},
                
                # Annotations and Extended Properties
                'Annotations': {'type': 'AnnotationCollection', 'description': 'Custom metadata annotations', 'editable': True},
                'ExtendedProperties': {'type': 'ExtendedPropertyCollection', 'description': 'Extended properties for Power BI', 'editable': True},
                
                # Dependencies and References
                'DependsOn': {'type': 'DependsOnCollection', 'description': 'Objects this measure depends on', 'editable': False},
                'ReferencedBy': {'type': 'ReferencedByCollection', 'description': 'Objects that reference this measure', 'editable': False}
            }
            
            result = {
                'measure_info': {
                    'table_name': table_name,
                    'measure_name': measure_name,
                    'measure_type': str(type(measure).__name__)
                },
                'available_properties': {},
                'current_values': {},
                'editable_properties': [],
                'readonly_properties': []
            }
            
            # Check each property and get its current value
            for prop_name, prop_info in property_definitions.items():
                try:
                    if hasattr(measure, prop_name):
                        current_value = getattr(measure, prop_name)
                        
                        # Convert complex objects to string representation
                        if current_value is not None:
                            if hasattr(current_value, 'Name'):  # For referenced objects
                                display_value = f"Reference: {current_value.Name}"
                            elif hasattr(current_value, '__str__') and not isinstance(current_value, (str, int, float, bool)):
                                display_value = str(current_value)
                            else:
                                display_value = current_value
                        else:
                            display_value = None
                        
                        result['available_properties'][prop_name] = prop_info
                        result['current_values'][prop_name] = display_value
                        
                        if prop_info['editable']:
                            result['editable_properties'].append(prop_name)
                        else:
                            result['readonly_properties'].append(prop_name)
                    else:
                        result['available_properties'][prop_name] = {
                            **prop_info,
                            'status': 'Not available in this version'
                        }
                except Exception as e:
                    result['available_properties'][prop_name] = {
                        **prop_info,
                        'status': f'Error accessing: {str(e)}'
                    }
            
            logger.info(f"Retrieved {len(result['available_properties'])} properties for measure '{measure_name}' in table '{table_name}'")
            return result
            
        except Exception as e:
            logger.error(f"Failed to get measure properties: {e}")
            raise Exception(f"Failed to get measure properties: {e}")
    
    def update_column_properties(self, table_name: str, column_name: str, 
                               properties: Dict[str, Any]) -> Dict[str, str]:
        """
        Update multiple properties of a column efficiently.
        
        Args:
            table_name: Name of the table containing the column
            column_name: Name of the column to update
            properties: Dictionary of property_name: value pairs to update
            
        Returns:
            Dictionary of results for each property update
        """
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        
        try:
            # Find the table
            table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
            if not table:
                raise Exception(f"Table '{table_name}' not found in the model.")
            
            # Find the column
            column = next((c for c in table.Columns if c.Name.lower() == column_name.lower()), None)
            if not column:
                raise Exception(f"Column '{column_name}' not found in table '{table_name}'.")
            
            results = {}
            readonly_props = ['IsCalculated', 'ModifiedTime', 'RefreshedTime', 'StructureModifiedTime', 'State']
            
            for prop_name, new_value in properties.items():
                try:
                    if not hasattr(column, prop_name):
                        results[prop_name] = f"❌ Property '{prop_name}' not available for columns"
                        continue
                    
                    # Check if property is read-only
                    if prop_name in readonly_props:
                        results[prop_name] = f"❌ Property '{prop_name}' is read-only"
                        continue
                    
                    # Get current value for comparison
                    current_value = getattr(column, prop_name, None)
                    
                    # Skip if value hasn't changed
                    if current_value == new_value:
                        results[prop_name] = f"✓ No change needed (already {new_value})"
                        continue
                    
                    # Set the property
                    setattr(column, prop_name, new_value)
                    results[prop_name] = f"✅ Updated from '{current_value}' to '{new_value}'"
                    
                except Exception as e:
                    results[prop_name] = f"❌ Error: {str(e)}"
            
            # Save changes if any properties were updated
            if any("✅" in result for result in results.values()):
                self.model.SaveChanges()
                logger.info(f"Saved property updates for column '{column_name}' in table '{table_name}'")
            
            return results
            
        except Exception as e:
            logger.error(f"Failed to update column properties: {e}")
            raise Exception(f"Failed to update column properties: {e}")
    
    def update_measure_properties(self, table_name: str, measure_name: str, 
                                properties: Dict[str, Any]) -> Dict[str, str]:
        """
        Update multiple properties of a measure efficiently.
        
        Args:
            table_name: Name of the table containing the measure
            measure_name: Name of the measure to update
            properties: Dictionary of property_name: value pairs to update
            
        Returns:
            Dictionary of results for each property update
        """
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        
        try:
            # Find the table
            table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
            if not table:
                raise Exception(f"Table '{table_name}' not found in the model.")
            
            # Find the measure
            measure = next((m for m in table.Measures if m.Name.lower() == measure_name.lower()), None)
            if not measure:
                raise Exception(f"Measure '{measure_name}' not found in table '{table_name}'.")
            
            results = {}
            readonly_props = ['IsSimpleMeasure', 'ModifiedTime', 'RefreshedTime', 
                            'StructureModifiedTime', 'DependsOn', 'ReferencedBy', 'State']
            
            for prop_name, new_value in properties.items():
                try:
                    if not hasattr(measure, prop_name):
                        results[prop_name] = f"❌ Property '{prop_name}' not available for measures"
                        continue
                    
                    # Check if property is read-only
                    if prop_name in readonly_props:
                        results[prop_name] = f"❌ Property '{prop_name}' is read-only"
                        continue
                    
                    # Get current value for comparison
                    current_value = getattr(measure, prop_name, None)
                    
                    # Skip if value hasn't changed
                    if current_value == new_value:
                        results[prop_name] = f"✓ No change needed (already {new_value})"
                        continue
                    
                    # Set the property
                    setattr(measure, prop_name, new_value)
                    results[prop_name] = f"✅ Updated from '{current_value}' to '{new_value}'"
                    
                except Exception as e:
                    results[prop_name] = f"❌ Error: {str(e)}"
            
            # Save changes if any properties were updated
            if any("✅" in result for result in results.values()):
                self.model.SaveChanges()
                logger.info(f"Saved property updates for measure '{measure_name}' in table '{table_name}'")
            
            return results
            
        except Exception as e:
            logger.error(f"Failed to update measure properties: {e}")
            raise Exception(f"Failed to update measure properties: {e}")
    
    def add_measure_annotations(self, table_name: str, measure_name: str = None, 
                               annotations: Dict[str, str] = None) -> Dict[str, str]:
        """
        Add custom annotations to measures for classification and metadata.
        Now supports both single measure and all measures in a table with auto-classification.
        
        Args:
            table_name: Name of the table containing the measure(s)
            measure_name: Name of the specific measure to annotate (optional - if not provided, applies to all measures in table)
            annotations: Dictionary of annotation_name: value pairs to add
            
        Returns:
            Dictionary of results for each annotation added
        """
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        
        try:
            # Find the table
            table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
            if not table:
                raise Exception(f"Table '{table_name}' not found in the model.")
            
            results = {}
            total_annotations_added = 0
            
            # Determine which measures to process
            measures_to_process = []
            if measure_name:
                # Single measure specified
                measure = next((m for m in table.Measures if m.Name.lower() == measure_name.lower()), None)
                if not measure:
                    raise Exception(f"Measure '{measure_name}' not found in table '{table_name}'.")
                measures_to_process.append(measure)
            else:
                # All measures in table
                measures_to_process = list(table.Measures)
            
            # Process each measure
            for measure in measures_to_process:
                measure_results = {}
                annotations_added = 0
                
                # If annotations provided, use them directly
                if annotations:
                    annotations_to_add = annotations.copy()
                else:
                    # Auto-classify the measure based on DAX expression
                    annotations_to_add = self._auto_classify_measure(measure)
                
                if not annotations_to_add:
                    measure_results["result"] = "❌ No annotations to add"
                    results[measure.Name] = measure_results
                    continue
                
                # Add/update annotations for this measure
                for annotation_name, annotation_value in annotations_to_add.items():
                    try:
                        # Check if annotation already exists
                        existing_annotation = None
                        for ann in measure.Annotations:
                            if ann.Name == annotation_name:
                                existing_annotation = ann
                                break
                        
                        if existing_annotation:
                            # Update existing annotation
                            old_value = existing_annotation.Value
                            existing_annotation.Value = str(annotation_value)
                            measure_results[annotation_name] = f"✅ Updated from '{old_value}' to '{annotation_value}'"
                        else:
                            # Create new annotation
                            new_annotation = Annotation()
                            new_annotation.Name = annotation_name
                            new_annotation.Value = str(annotation_value)
                            measure.Annotations.Add(new_annotation)
                            measure_results[annotation_name] = f"✅ Added '{annotation_name}' = '{annotation_value}'"
                        
                        annotations_added += 1
                        total_annotations_added += 1
                        
                    except Exception as e:
                        measure_results[annotation_name] = f"❌ Error: {str(e)}"
                
                measure_results["annotations_count"] = annotations_added
                results[measure.Name] = measure_results
            
            # Save changes if any annotations were added
            if total_annotations_added > 0:
                self.model.SaveChanges()
                logger.info(f"Added {total_annotations_added} annotations to {len(measures_to_process)} measures in table '{table_name}'")
                results["summary"] = f"✅ Successfully processed {len(measures_to_process)} measures with {total_annotations_added} total annotations"
            else:
                results["summary"] = "❌ No annotations were successfully added"
            
            return results
            
        except Exception as e:
            logger.error(f"Failed to add measure annotations: {e}")
            raise Exception(f"Failed to add measure annotations: {e}")

    def _auto_classify_measure(self, measure) -> Dict[str, str]:
        """
        Automatically classify a measure based on its DAX expression and properties.
        
        Args:
            measure: The measure object to classify
            
        Returns:
            Dictionary of annotations to add
        """
        try:
            expression = measure.Expression.upper().strip() if measure.Expression else ""
            measure_name = measure.Name
            
            annotations = {}
            
            # Analyze DAX expression patterns for classification
            if self._is_simple_aggregation(expression):
                annotations["Custom Classification"] = "Simple measure"
                annotations["Complexity Level"] = "Low"
                annotations["Category"] = self._get_aggregation_category(expression)
            elif self._is_calculated_measure(expression):
                annotations["Custom Classification"] = "Calculated measure"
                annotations["Complexity Level"] = self._get_complexity_level(expression)
                annotations["Category"] = "Calculated"
            elif self._is_time_intelligence(expression):
                annotations["Custom Classification"] = "Time intelligence"
                annotations["Complexity Level"] = "Medium"
                annotations["Category"] = "Time intelligence"
            elif self._is_ratio_percentage(expression):
                annotations["Custom Classification"] = "Ratio/Percentage"
                annotations["Complexity Level"] = "Medium"
                annotations["Category"] = "Performance metric"
            else:
                # Default classification for complex measures
                annotations["Custom Classification"] = "Complex measure"
                annotations["Complexity Level"] = "High"
                annotations["Category"] = "Advanced calculation"
            
            # Additional context-based classification
            if "PIPELINE" in measure_name.upper() or "VALUE" in measure_name.upper():
                annotations["Business Domain"] = "Sales Pipeline"
            elif "DEAL" in measure_name.upper() or "COUNT" in measure_name.upper():
                annotations["Business Domain"] = "Sales Metrics"
            elif "RATE" in measure_name.upper() or "PERCENTAGE" in measure_name.upper():
                annotations["Business Domain"] = "Performance KPI"
            elif "AVERAGE" in measure_name.upper() or "AVG" in measure_name.upper():
                annotations["Business Domain"] = "Statistical Metric"
            
            return annotations
            
        except Exception as e:
            logger.error(f"Error auto-classifying measure {measure.Name}: {e}")
            return {}

    def _is_simple_aggregation(self, expression: str) -> bool:
        """Check if expression is a simple aggregation function"""
        simple_patterns = [
            r'^SUM\s*\(',
            r'^COUNT\s*\(',
            r'^COUNTROWS\s*\(',
            r'^AVERAGE\s*\(',
            r'^MIN\s*\(',
            r'^MAX\s*\(',
            r'^DISTINCTCOUNT\s*\(',
            r'^VALUES\s*\(',
        ]
        
        import re
        for pattern in simple_patterns:
            if re.match(pattern, expression):
                return True
        return False

    def _is_calculated_measure(self, expression: str) -> bool:
        """Check if expression is a calculated measure (uses DIVIDE, mathematical operations)"""
        calculated_patterns = [
            r'DIVIDE\s*\(',
            r'[\+\-\*\/]',  # Mathematical operators
            r'\[.*\]\s*[\+\-\*\/]\s*\[.*\]',  # Measure references with math
        ]
        
        import re
        for pattern in calculated_patterns:
            if re.search(pattern, expression):
                return True
        return False

    def _is_time_intelligence(self, expression: str) -> bool:
        """Check if expression uses time intelligence functions"""
        time_patterns = [
            r'DATEADD\s*\(',
            r'DATESYTD\s*\(',
            r'DATESMTD\s*\(',
            r'DATESQTD\s*\(',
            r'SAMEPERIODLASTYEAR\s*\(',
            r'PARALLELPERIOD\s*\(',
            r'PREVIOUSMONTH\s*\(',
            r'PREVIOUSYEAR\s*\(',
        ]
        
        import re
        for pattern in time_patterns:
            if re.search(pattern, expression):
                return True
        return False

    def _is_ratio_percentage(self, expression: str) -> bool:
        """Check if expression calculates ratios or percentages"""
        return ("DIVIDE" in expression and 
                (any(word in expression for word in ["RATE", "PERCENTAGE", "%", "RATIO"])))

    def _get_aggregation_category(self, expression: str) -> str:
        """Determine the specific category of aggregation"""
        if "SUM(" in expression:
            return "Basic aggregation"
        elif any(func in expression for func in ["COUNT(", "COUNTROWS("]):
            return "Row count"
        elif "AVERAGE(" in expression:
            return "Basic aggregation"
        elif "DISTINCTCOUNT(" in expression:
            return "Distinct count"
        elif any(func in expression for func in ["MIN(", "MAX("]):
            return "Basic aggregation"
        else:
            return "Basic aggregation"

    def _get_complexity_level(self, expression: str) -> str:
        """Determine complexity level based on expression analysis"""
        complexity_indicators = [
            ("CALCULATE(", 1),
            ("FILTER(", 2),
            ("SUMX(", 2),
            ("AVERAGEX(", 2),
            ("IF(", 1),
            ("SWITCH(", 2),
            ("VAR ", 2),
            ("RETURN", 2),
            ("RELATED(", 1),
            ("RELATEDTABLE(", 2),
        ]
        
        complexity_score = 0
        for pattern, score in complexity_indicators:
            if pattern in expression:
                complexity_score += score
        
        if complexity_score == 0:
            return "Low"
        elif complexity_score <= 2:
            return "Medium"
        else:
            return "High"

    def classify_all_measures_in_model(self) -> Dict[str, Any]:
        """
        Automatically classify all measures in the entire model with intelligent annotation assignment.
        
        Returns:
            Dictionary with classification results for all measures
        """
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        
        try:
            results = {
                "tables_processed": 0,
                "measures_processed": 0,
                "total_annotations_added": 0,
                "classification_summary": {},
                "details": {}
            }
            
            # Process all tables in the model
            for table in self.model.Tables:
                if not table.Measures or len(table.Measures) == 0:
                    continue
                
                table_results = {
                    "measures": {},
                    "measure_count": len(table.Measures),
                    "annotations_added": 0
                }
                
                # Process each measure in the table
                for measure in table.Measures:
                    measure_results = {}
                    annotations_added = 0
                    
                    # Auto-classify the measure
                    annotations_to_add = self._auto_classify_measure(measure)
                    
                    if not annotations_to_add:
                        measure_results["result"] = "❌ No classification determined"
                        table_results["measures"][measure.Name] = measure_results
                        continue
                    
                    # Add/update annotations for this measure
                    for annotation_name, annotation_value in annotations_to_add.items():
                        try:
                            # Check if annotation already exists
                            existing_annotation = None
                            for ann in measure.Annotations:
                                if ann.Name == annotation_name:
                                    existing_annotation = ann
                                    break
                            
                            if existing_annotation:
                                # Update existing annotation
                                old_value = existing_annotation.Value
                                existing_annotation.Value = str(annotation_value)
                                measure_results[annotation_name] = f"✅ Updated from '{old_value}' to '{annotation_value}'"
                            else:
                                # Create new annotation
                                new_annotation = Annotation()
                                new_annotation.Name = annotation_name
                                new_annotation.Value = str(annotation_value)
                                measure.Annotations.Add(new_annotation)
                                measure_results[annotation_name] = f"✅ Added '{annotation_name}' = '{annotation_value}'"
                            
                            annotations_added += 1
                            table_results["annotations_added"] += 1
                            results["total_annotations_added"] += 1
                            
                            # Track classification summary
                            if annotation_name == "Custom Classification":
                                if annotation_value not in results["classification_summary"]:
                                    results["classification_summary"][annotation_value] = 0
                                results["classification_summary"][annotation_value] += 1
                            
                        except Exception as e:
                            measure_results[annotation_name] = f"❌ Error: {str(e)}"
                    
                    measure_results["annotations_count"] = annotations_added
                    measure_results["dax_expression"] = measure.Expression[:100] + "..." if len(measure.Expression) > 100 else measure.Expression
                    table_results["measures"][measure.Name] = measure_results
                    results["measures_processed"] += 1
                
                results["details"][table.Name] = table_results
                results["tables_processed"] += 1
            
            # Save all changes
            if results["total_annotations_added"] > 0:
                self.model.SaveChanges()
                logger.info(f"Auto-classified {results['measures_processed']} measures across {results['tables_processed']} tables with {results['total_annotations_added']} total annotations")
                results["status"] = f"✅ Successfully classified {results['measures_processed']} measures with {results['total_annotations_added']} annotations"
            else:
                results["status"] = "❌ No measures were successfully classified"
            
            return results
            
        except Exception as e:
            logger.error(f"Failed to classify all measures: {e}")
            raise Exception(f"Failed to classify all measures: {e}")

    def analyze_dependencies(self, object_type: str, object_name: str, table_name: str = None) -> Dict[str, Any]:
        """
        Analyze dependencies for a given object (table, column, or measure) before renaming.
        
        Args:
            object_type: Type of object ('table', 'column', 'measure')
            object_name: Name of the object to analyze
            table_name: Name of the table (required for column and measure)
            
        Returns:
            Dictionary with dependency analysis results
        """
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        
        try:
            dependencies = {
                "object_info": {
                    "type": object_type,
                    "name": object_name,
                    "table_name": table_name
                },
                "dependent_measures": [],
                "dependent_calculated_columns": [],
                "dependent_relationships": [],
                "dependent_table_security_roles": [],
                "impact_summary": {
                    "total_objects_affected": 0,
                    "risk_level": "LOW",
                    "recommendations": []
                }
            }
            
            if object_type.lower() == "table":
                dependencies = self._analyze_table_dependencies(object_name, dependencies)
            elif object_type.lower() == "column":
                dependencies = self._analyze_column_dependencies(table_name, object_name, dependencies)
            elif object_type.lower() == "measure":
                dependencies = self._analyze_measure_dependencies(table_name, object_name, dependencies)
            else:
                raise Exception(f"Unsupported object type: {object_type}")
            
            # Calculate risk level and recommendations
            dependencies = self._calculate_risk_level(dependencies)
            
            logger.info(f"Dependency analysis completed for {object_type} '{object_name}'. Found {dependencies['impact_summary']['total_objects_affected']} dependent objects.")
            return dependencies
            
        except Exception as e:
            logger.error(f"Failed to analyze dependencies: {e}")
            raise Exception(f"Failed to analyze dependencies: {e}")

    def _analyze_table_dependencies(self, table_name: str, dependencies: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze dependencies for a table."""
        table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
        if not table:
            raise Exception(f"Table '{table_name}' not found.")
        
        # Check measures in other tables that reference this table
        for other_table in self.model.Tables:
            for measure in other_table.Measures:
                if measure.Expression and re.search(rf'\b{re.escape(table_name)}\s*\[', measure.Expression):
                    dependencies["dependent_measures"].append({
                        "table": other_table.Name,
                        "measure": measure.Name,
                        "expression": measure.Expression[:200] + "..." if len(measure.Expression) > 200 else measure.Expression,
                        "impact": "DAX expression contains table reference"
                    })
        
        # Check calculated columns in other tables
        for other_table in self.model.Tables:
            for column in other_table.Columns:
                if hasattr(column, 'Expression') and column.Expression:
                    if re.search(rf'\b{re.escape(table_name)}\s*\[', column.Expression):
                        dependencies["dependent_calculated_columns"].append({
                            "table": other_table.Name,
                            "column": column.Name,
                            "expression": column.Expression[:200] + "..." if len(column.Expression) > 200 else column.Expression,
                            "impact": "Calculated column expression contains table reference"
                        })
        
        # Check relationships
        for relationship in self.model.Relationships:
            if (relationship.FromTable.Name.lower() == table_name.lower() or 
                relationship.ToTable.Name.lower() == table_name.lower()):
                dependencies["dependent_relationships"].append({
                    "name": relationship.Name,
                    "from_table": relationship.FromTable.Name,
                    "from_column": relationship.FromColumn.Name,
                    "to_table": relationship.ToTable.Name,
                    "to_column": relationship.ToColumn.Name,
                    "is_active": relationship.IsActive,
                    "impact": "Relationship involves this table"
                })
        
        # Check table security roles
        for role in self.model.Roles:
            for table_permission in role.TablePermissions:
                if table_permission.Table.Name.lower() == table_name.lower():
                    dependencies["dependent_table_security_roles"].append({
                        "role_name": role.Name,
                        "table": table_permission.Table.Name,
                        "filter_expression": getattr(table_permission, 'FilterExpression', 'No filter'),
                        "impact": "Role has permissions on this table"
                    })
        
        return dependencies

    def _analyze_column_dependencies(self, table_name: str, column_name: str, dependencies: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze dependencies for a column."""
        table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
        if not table:
            raise Exception(f"Table '{table_name}' not found.")
        
        column = next((c for c in table.Columns if c.Name.lower() == column_name.lower()), None)
        if not column:
            raise Exception(f"Column '{column_name}' not found in table '{table_name}'.")
        
        # Check measures that reference this column
        for search_table in self.model.Tables:
            for measure in search_table.Measures:
                if measure.Expression:
                    # Look for both Table[Column] and [Column] references
                    column_patterns = [
                        rf'\b{re.escape(table_name)}\s*\[\s*{re.escape(column_name)}\s*\]',
                        rf'(?<!\w)\[\s*{re.escape(column_name)}\s*\]' if search_table.Name == table_name else None
                    ]
                    
                    for pattern in column_patterns:
                        if pattern and re.search(pattern, measure.Expression):
                            dependencies["dependent_measures"].append({
                                "table": search_table.Name,
                                "measure": measure.Name,
                                "expression": measure.Expression[:200] + "..." if len(measure.Expression) > 200 else measure.Expression,
                                "impact": "DAX expression references this column"
                            })
                            break
        
        # Check calculated columns that reference this column
        for search_table in self.model.Tables:
            for calc_column in search_table.Columns:
                if hasattr(calc_column, 'Expression') and calc_column.Expression:
                    column_patterns = [
                        rf'\b{re.escape(table_name)}\s*\[\s*{re.escape(column_name)}\s*\]',
                        rf'(?<!\w)\[\s*{re.escape(column_name)}\s*\]' if search_table.Name == table_name else None
                    ]
                    
                    for pattern in column_patterns:
                        if pattern and re.search(pattern, calc_column.Expression):
                            dependencies["dependent_calculated_columns"].append({
                                "table": search_table.Name,
                                "column": calc_column.Name,
                                "expression": calc_column.Expression[:200] + "..." if len(calc_column.Expression) > 200 else calc_column.Expression,
                                "impact": "Calculated column expression references this column"
                            })
                            break
        
        # Check relationships involving this column
        for relationship in self.model.Relationships:
            if ((relationship.FromTable.Name.lower() == table_name.lower() and 
                 relationship.FromColumn.Name.lower() == column_name.lower()) or
                (relationship.ToTable.Name.lower() == table_name.lower() and 
                 relationship.ToColumn.Name.lower() == column_name.lower())):
                dependencies["dependent_relationships"].append({
                    "name": relationship.Name,
                    "from_table": relationship.FromTable.Name,
                    "from_column": relationship.FromColumn.Name,
                    "to_table": relationship.ToTable.Name,
                    "to_column": relationship.ToColumn.Name,
                    "is_active": relationship.IsActive,
                    "impact": "Relationship uses this column"
                })
        
        # Check if column is used in sort by relationships
        for search_table in self.model.Tables:
            for other_column in search_table.Columns:
                if hasattr(other_column, 'SortByColumn') and other_column.SortByColumn:
                    if (other_column.SortByColumn.Table.Name.lower() == table_name.lower() and
                        other_column.SortByColumn.Name.lower() == column_name.lower()):
                        dependencies["dependent_calculated_columns"].append({
                            "table": search_table.Name,
                            "column": other_column.Name,
                            "expression": f"SortBy: {table_name}[{column_name}]",
                            "impact": "Column uses this column for sorting"
                        })
        
        return dependencies

    def _analyze_measure_dependencies(self, table_name: str, measure_name: str, dependencies: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze dependencies for a measure."""
        table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
        if not table:
            raise Exception(f"Table '{table_name}' not found.")
        
        measure = next((m for m in table.Measures if m.Name.lower() == measure_name.lower()), None)
        if not measure:
            raise Exception(f"Measure '{measure_name}' not found in table '{table_name}'.")
        
        # Check other measures that reference this measure
        for search_table in self.model.Tables:
            for other_measure in search_table.Measures:
                if other_measure.Name != measure_name and other_measure.Expression:
                    # Look for [MeasureName] references
                    if re.search(rf'(?<!\w)\[\s*{re.escape(measure_name)}\s*\]', other_measure.Expression):
                        dependencies["dependent_measures"].append({
                            "table": search_table.Name,
                            "measure": other_measure.Name,
                            "expression": other_measure.Expression[:200] + "..." if len(other_measure.Expression) > 200 else other_measure.Expression,
                            "impact": "DAX expression references this measure"
                        })
        
        # Check calculated columns that reference this measure
        for search_table in self.model.Tables:
            for calc_column in search_table.Columns:
                if hasattr(calc_column, 'Expression') and calc_column.Expression:
                    if re.search(rf'(?<!\w)\[\s*{re.escape(measure_name)}\s*\]', calc_column.Expression):
                        dependencies["dependent_calculated_columns"].append({
                            "table": search_table.Name,
                            "column": calc_column.Name,
                            "expression": calc_column.Expression[:200] + "..." if len(calc_column.Expression) > 200 else calc_column.Expression,
                            "impact": "Calculated column expression references this measure"
                        })
        
        return dependencies

    def _calculate_risk_level(self, dependencies: Dict[str, Any]) -> Dict[str, Any]:
        """Calculate risk level and provide recommendations based on dependencies."""
        total_affected = (len(dependencies["dependent_measures"]) + 
                         len(dependencies["dependent_calculated_columns"]) + 
                         len(dependencies["dependent_relationships"]) + 
                         len(dependencies["dependent_table_security_roles"]))
        
        dependencies["impact_summary"]["total_objects_affected"] = total_affected
        
        # Determine risk level
        if total_affected == 0:
            dependencies["impact_summary"]["risk_level"] = "NONE"
            dependencies["impact_summary"]["recommendations"] = [
                "✅ Safe to rename - no dependencies found"
            ]
        elif total_affected <= 5:
            dependencies["impact_summary"]["risk_level"] = "LOW"
            dependencies["impact_summary"]["recommendations"] = [
                "⚠️ Low risk - few dependencies found",
                "Review dependent objects before proceeding",
                "Consider testing in development environment first"
            ]
        elif total_affected <= 15:
            dependencies["impact_summary"]["risk_level"] = "MEDIUM"
            dependencies["impact_summary"]["recommendations"] = [
                "⚠️ Medium risk - multiple dependencies found",
                "Carefully review all dependent DAX expressions",
                "Test thoroughly in development environment",
                "Consider notifying downstream users"
            ]
        else:
            dependencies["impact_summary"]["risk_level"] = "HIGH"
            dependencies["impact_summary"]["recommendations"] = [
                "🚨 High risk - many dependencies found",
                "Plan rename operation carefully",
                "Create backup before proceeding",
                "Test extensively in development environment",
                "Coordinate with all stakeholders",
                "Consider phased rollout approach"
            ]
        
        return dependencies

    def safe_rename_with_dependencies(self, object_type: str, old_name: str, new_name: str, 
                                    table_name: str = None, confirmed: bool = False) -> Dict[str, Any]:
        """
        Safely rename an object (table, column, or measure) and update all dependencies.
        
        Args:
            object_type: Type of object ('table', 'column', 'measure')
            old_name: Current name of the object
            new_name: New name for the object
            table_name: Name of the table (required for column and measure)
            confirmed: Whether user has confirmed the operation
            
        Returns:
            Dictionary with operation results
        """
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        
        try:
            # First, analyze dependencies
            dependencies = self.analyze_dependencies(object_type, old_name, table_name)
            
            result = {
                "operation": f"Rename {object_type} '{old_name}' to '{new_name}'",
                "dependencies_analyzed": dependencies,
                "confirmation_required": not confirmed,
                "updates_performed": [],
                "status": "pending_confirmation"
            }
            
            # If not confirmed, return dependency analysis for user review
            if not confirmed:
                result["message"] = "⚠️ Please review dependencies and confirm the operation"
                result["next_steps"] = [
                    "Review the dependency analysis carefully",
                    "Ensure all affected objects are acceptable to modify", 
                    "Call this function again with confirmed=True to proceed"
                ]
                return result
            
            # User confirmed - proceed with rename operation
            result["status"] = "executing"
            
            if object_type.lower() == "table":
                rename_result = self._safe_rename_table(old_name, new_name, dependencies)
            elif object_type.lower() == "column":
                rename_result = self._safe_rename_column(table_name, old_name, new_name, dependencies)
            elif object_type.lower() == "measure":
                rename_result = self._safe_rename_measure(table_name, old_name, new_name, dependencies)
            else:
                raise Exception(f"Unsupported object type: {object_type}")
            
            result.update(rename_result)
            result["status"] = "completed"
            
            logger.info(f"Successfully renamed {object_type} '{old_name}' to '{new_name}' with {len(result['updates_performed'])} dependency updates")
            return result
            
        except Exception as e:
            logger.error(f"Failed to safely rename {object_type}: {e}")
            raise Exception(f"Failed to safely rename {object_type}: {e}")

    def _safe_rename_table(self, old_name: str, new_name: str, dependencies: Dict[str, Any]) -> Dict[str, Any]:
        """Safely rename a table and update all dependencies."""
        # Find and rename the table
        table = next((t for t in self.model.Tables if t.Name.lower() == old_name.lower()), None)
        if not table:
            raise Exception(f"Table '{old_name}' not found.")
        
        if any(t.Name.lower() == new_name.lower() for t in self.model.Tables):
            raise Exception(f"A table named '{new_name}' already exists.")
        
        updates_performed = []
        
        # Update dependent measures
        for measure_dep in dependencies["dependent_measures"]:
            measure_table = next((t for t in self.model.Tables if t.Name == measure_dep["table"]), None)
            if measure_table:
                measure = next((m for m in measure_table.Measures if m.Name == measure_dep["measure"]), None)
                if measure and measure.Expression:
                    old_expression = measure.Expression
                    new_expression = re.sub(rf'\b{re.escape(old_name)}\s*\[', f"{new_name}[", old_expression)
                    measure.Expression = new_expression
                    updates_performed.append(f"Updated measure {measure_table.Name}[{measure.Name}]")
        
        # Update dependent calculated columns
        for column_dep in dependencies["dependent_calculated_columns"]:
            calc_table = next((t for t in self.model.Tables if t.Name == column_dep["table"]), None)
            if calc_table:
                calc_column = next((c for c in calc_table.Columns if c.Name == column_dep["column"]), None)
                if calc_column and hasattr(calc_column, 'Expression') and calc_column.Expression:
                    old_expression = calc_column.Expression
                    new_expression = re.sub(rf'\b{re.escape(old_name)}\s*\[', f"{new_name}[", old_expression)
                    calc_column.Expression = new_expression
                    updates_performed.append(f"Updated calculated column {calc_table.Name}[{calc_column.Name}]")
        
        # Rename the table itself
        table.Name = new_name
        updates_performed.append(f"Renamed table '{old_name}' to '{new_name}'")
        
        # Save changes
        self.model.RequestRefresh(RefreshType.Automatic)
        self.model.SaveChanges()
        
        return {
            "updates_performed": updates_performed,
            "table_renamed": True,
            "refresh_triggered": True
        }

    def _safe_rename_column(self, table_name: str, old_name: str, new_name: str, dependencies: Dict[str, Any]) -> Dict[str, Any]:
        """Safely rename a column and update all dependencies."""
        table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
        if not table:
            raise Exception(f"Table '{table_name}' not found.")
        
        column = next((c for c in table.Columns if c.Name.lower() == old_name.lower()), None)
        if not column:
            raise Exception(f"Column '{old_name}' not found in table '{table_name}'.")
        
        if any(c.Name.lower() == new_name.lower() for c in table.Columns):
            raise Exception(f"A column named '{new_name}' already exists in table '{table_name}'.")
        
        updates_performed = []
        
        # Update dependent measures
        for measure_dep in dependencies["dependent_measures"]:
            measure_table = next((t for t in self.model.Tables if t.Name == measure_dep["table"]), None)
            if measure_table:
                measure = next((m for m in measure_table.Measures if m.Name == measure_dep["measure"]), None)
                if measure and measure.Expression:
                    old_expression = measure.Expression
                    # Update both Table[Column] and [Column] patterns
                    new_expression = re.sub(rf'\b{re.escape(table_name)}\s*\[\s*{re.escape(old_name)}\s*\]', 
                                          f"{table_name}[{new_name}]", old_expression)
                    if measure_table.Name == table_name:
                        new_expression = re.sub(rf'(?<!\w)\[\s*{re.escape(old_name)}\s*\]', 
                                              f"[{new_name}]", new_expression)
                    measure.Expression = new_expression
                    updates_performed.append(f"Updated measure {measure_table.Name}[{measure.Name}]")
        
        # Update dependent calculated columns
        for column_dep in dependencies["dependent_calculated_columns"]:
            calc_table = next((t for t in self.model.Tables if t.Name == column_dep["table"]), None)
            if calc_table:
                calc_column = next((c for c in calc_table.Columns if c.Name == column_dep["column"]), None)
                if calc_column and hasattr(calc_column, 'Expression') and calc_column.Expression:
                    old_expression = calc_column.Expression
                    new_expression = re.sub(rf'\b{re.escape(table_name)}\s*\[\s*{re.escape(old_name)}\s*\]', 
                                          f"{table_name}[{new_name}]", old_expression)
                    if calc_table.Name == table_name:
                        new_expression = re.sub(rf'(?<!\w)\[\s*{re.escape(old_name)}\s*\]', 
                                              f"[{new_name}]", new_expression)
                    calc_column.Expression = new_expression
                    updates_performed.append(f"Updated calculated column {calc_table.Name}[{calc_column.Name}]")
        
        # Update relationships (will be handled automatically by the server when column is renamed)
        
        # Rename the column itself
        column.Name = new_name
        updates_performed.append(f"Renamed column '{table_name}[{old_name}]' to '{table_name}[{new_name}]'")
        
        # Save changes
        self.model.RequestRefresh(RefreshType.Automatic)
        self.model.SaveChanges()
        
        return {
            "updates_performed": updates_performed,
            "column_renamed": True,
            "refresh_triggered": True
        }

    def _safe_rename_measure(self, table_name: str, old_name: str, new_name: str, dependencies: Dict[str, Any]) -> Dict[str, Any]:
        """Safely rename a measure and update all dependencies."""
        table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
        if not table:
            raise Exception(f"Table '{table_name}' not found.")
        
        measure = next((m for m in table.Measures if m.Name.lower() == old_name.lower()), None)
        if not measure:
            raise Exception(f"Measure '{old_name}' not found in table '{table_name}'.")
        
        if any(m.Name.lower() == new_name.lower() for m in table.Measures):
            raise Exception(f"A measure named '{new_name}' already exists in table '{table_name}'.")
        
        updates_performed = []
        
        # Update dependent measures
        for measure_dep in dependencies["dependent_measures"]:
            dep_table = next((t for t in self.model.Tables if t.Name == measure_dep["table"]), None)
            if dep_table:
                dep_measure = next((m for m in dep_table.Measures if m.Name == measure_dep["measure"]), None)
                if dep_measure and dep_measure.Expression:
                    old_expression = dep_measure.Expression
                    new_expression = re.sub(rf'(?<!\w)\[\s*{re.escape(old_name)}\s*\]', 
                                          f"[{new_name}]", old_expression)
                    dep_measure.Expression = new_expression
                    updates_performed.append(f"Updated measure {dep_table.Name}[{dep_measure.Name}]")
        
        # Update dependent calculated columns
        for column_dep in dependencies["dependent_calculated_columns"]:
            calc_table = next((t for t in self.model.Tables if t.Name == column_dep["table"]), None)
            if calc_table:
                calc_column = next((c for c in calc_table.Columns if c.Name == column_dep["column"]), None)
                if calc_column and hasattr(calc_column, 'Expression') and calc_column.Expression:
                    old_expression = calc_column.Expression
                    new_expression = re.sub(rf'(?<!\w)\[\s*{re.escape(old_name)}\s*\]', 
                                          f"[{new_name}]", old_expression)
                    calc_column.Expression = new_expression
                    updates_performed.append(f"Updated calculated column {calc_table.Name}[{calc_column.Name}]")
        
        # Rename the measure itself
        measure.Name = new_name
        updates_performed.append(f"Renamed measure '{table_name}[{old_name}]' to '{table_name}[{new_name}]'")
        
        # Save changes
        self.model.SaveChanges()
        
        return {
            "updates_performed": updates_performed,
            "measure_renamed": True,
            "refresh_triggered": False
        }

    def get_table_properties(self, table_name: str) -> Dict[str, Any]:
        """
        Get all available properties for a specific table with their current values and metadata.
        
        Args:
            table_name: Name of the table to inspect
            
        Returns:
            Dictionary with property details including current values, types, and descriptions
        """
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        
        try:
            # Find the table
            table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
            if not table:
                raise Exception(f"Table '{table_name}' not found in the model.")
            
            # Define all possible table properties with their metadata
            property_definitions = {
                # Core Properties
                'Name': {'type': 'string', 'description': 'The name of the table', 'editable': True},
                'Description': {'type': 'string', 'description': 'Description of the table', 'editable': True},
                
                # Visibility and Behavior
                'IsHidden': {'type': 'boolean', 'description': 'Whether table is hidden from client tools', 'editable': True},
                'IsPrivate': {'type': 'boolean', 'description': 'Whether table is private (for calculated tables)', 'editable': True},
                
                # Data Source Properties
                'Source': {'type': 'PartitionSource', 'description': 'Data source configuration for the table', 'editable': False},
                'Mode': {'type': 'ModeType', 'description': 'Storage mode (Import, DirectQuery, Dual, DirectLake)', 'editable': False},
                
                # Data Category and Organization
                'DataCategory': {'type': 'string', 'description': 'Data category (e.g., "Time", "Geography")', 'editable': True},
                'DisplayFolder': {'type': 'string', 'description': 'Display folder in client tools', 'editable': True},
                'DisplayOrdinal': {'type': 'integer', 'description': 'Display order in client tools', 'editable': True},
                
                # Date Table Properties
                'DataView': {'type': 'DataViewType', 'description': 'Data view type (Default, Sample)', 'editable': True},
                
                # State and Metadata
                'State': {'type': 'ObjectState', 'description': 'Current state of the table', 'editable': False},
                'ModifiedTime': {'type': 'DateTime', 'description': 'Last modification time', 'editable': False},
                'RefreshedTime': {'type': 'DateTime', 'description': 'Last refresh time', 'editable': False},
                'StructureModifiedTime': {'type': 'DateTime', 'description': 'Structure modification time', 'editable': False},
                
                # Lineage Information
                'LineageTag': {'type': 'string', 'description': 'Unique lineage identifier', 'editable': True},
                'SourceLineageTag': {'type': 'string', 'description': 'Source lineage identifier', 'editable': True},
                
                # Collections (Read-only references)
                'Columns': {'type': 'ColumnCollection', 'description': 'Collection of columns in the table', 'editable': False},
                'Measures': {'type': 'MeasureCollection', 'description': 'Collection of measures in the table', 'editable': False},
                'Partitions': {'type': 'PartitionCollection', 'description': 'Collection of partitions in the table', 'editable': False},
                'Hierarchies': {'type': 'HierarchyCollection', 'description': 'Collection of hierarchies in the table', 'editable': False},
                
                # Annotations and Extended Properties
                'Annotations': {'type': 'AnnotationCollection', 'description': 'Custom metadata annotations', 'editable': True},
                'ExtendedProperties': {'type': 'ExtendedPropertyCollection', 'description': 'Extended properties for Power BI', 'editable': True},
                
                # Dependencies and References
                'DependsOn': {'type': 'DependsOnCollection', 'description': 'Objects this table depends on', 'editable': False},
                'ReferencedBy': {'type': 'ReferencedByCollection', 'description': 'Objects that reference this table', 'editable': False},
                
                # Advanced Properties
                'ShowAsVariationsOnly': {'type': 'boolean', 'description': 'Whether to show only as variations', 'editable': True},
                'RequestId': {'type': 'string', 'description': 'Request ID for tracking', 'editable': False}
            }
            
            result = {
                'table_info': {
                    'table_name': table_name,
                    'table_type': str(type(table).__name__),
                    'column_count': len(table.Columns) if hasattr(table, 'Columns') else 0,
                    'measure_count': len(table.Measures) if hasattr(table, 'Measures') else 0
                },
                'available_properties': {},
                'current_values': {},
                'editable_properties': [],
                'readonly_properties': []
            }
            
            # Check each property and get its current value
            for prop_name, prop_info in property_definitions.items():
                try:
                    if hasattr(table, prop_name):
                        current_value = getattr(table, prop_name)
                        
                        # Convert complex objects to string representation
                        if current_value is not None:
                            if hasattr(current_value, 'Name'):  # For referenced objects
                                display_value = f"Reference: {current_value.Name}"
                            elif hasattr(current_value, 'Count'):  # For collections
                                display_value = f"Collection with {current_value.Count} items"
                            elif hasattr(current_value, '__str__') and not isinstance(current_value, (str, int, float, bool)):
                                display_value = str(current_value)
                            else:
                                display_value = current_value
                        else:
                            display_value = None
                        
                        result['available_properties'][prop_name] = prop_info
                        result['current_values'][prop_name] = display_value
                        
                        if prop_info['editable']:
                            result['editable_properties'].append(prop_name)
                        else:
                            result['readonly_properties'].append(prop_name)
                    else:
                        result['available_properties'][prop_name] = {
                            **prop_info,
                            'status': 'Not available in this version'
                        }
                except Exception as e:
                    result['available_properties'][prop_name] = {
                        **prop_info,
                        'status': f'Error accessing: {str(e)}'
                    }
            
            logger.info(f"Retrieved {len(result['available_properties'])} properties for table '{table_name}'")
            return result
            
        except Exception as e:
            logger.error(f"Failed to get table properties: {e}")
            raise Exception(f"Failed to get table properties: {e}")
    
    def update_table_properties(self, table_name: str, properties: Dict[str, Any]) -> Dict[str, str]:
        """
        Update multiple properties of a table efficiently.
        
        Args:
            table_name: Name of the table to update
            properties: Dictionary of property_name: value pairs to update
            
        Returns:
            Dictionary of results for each property update
        """
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        
        try:
            # Find the table
            table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
            if not table:
                raise Exception(f"Table '{table_name}' not found in the model.")
            
            results = {}
            readonly_props = ['Source', 'Mode', 'State', 'ModifiedTime', 'RefreshedTime', 
                            'StructureModifiedTime', 'Columns', 'Measures', 'Partitions', 
                            'Hierarchies', 'DependsOn', 'ReferencedBy', 'RequestId']
            
            for prop_name, new_value in properties.items():
                try:
                    if not hasattr(table, prop_name):
                        results[prop_name] = f"❌ Property '{prop_name}' not available for tables"
                        continue
                    
                    # Check if property is read-only
                    if prop_name in readonly_props:
                        results[prop_name] = f"❌ Property '{prop_name}' is read-only"
                        continue
                    
                    # Get current value for comparison
                    current_value = getattr(table, prop_name, None)
                    
                    # Skip if value hasn't changed
                    if current_value == new_value:
                        results[prop_name] = f"✓ No change needed (already {new_value})"
                        continue
                    
                    # Set the property
                    setattr(table, prop_name, new_value)
                    results[prop_name] = f"✅ Updated from '{current_value}' to '{new_value}'"
                    
                except Exception as e:
                    results[prop_name] = f"❌ Error: {str(e)}"
            
            # Save changes if any properties were updated
            if any("✅" in result for result in results.values()):
                self.model.SaveChanges()
                logger.info(f"Saved property updates for table '{table_name}'")
            
            return results
            
        except Exception as e:
            logger.error(f"Failed to update table properties: {e}")
            raise Exception(f"Failed to update table properties: {e}")

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
                name="create_relationship",
                description="Create a new relationship between two tables.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "from_table": {"type": "string"},
                        "from_column": {"type": "string"},
                        "to_table": {"type": "string"},
                        "to_column": {"type": "string"},
                        "is_active": {"type": "boolean"},
                        "cross_filter_direction": {"type": "string"}
                    },
                    "required": ["from_table", "from_column", "to_table", "to_column"]
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
            # REMOVED: update_column_names tool - use safe_rename_with_dependencies instead
            # REMOVED: update_table_name tool - use safe_rename_with_dependencies instead
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
                name="create_table_security_role",
                description="Create a new table security role.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "role_name": {"type": "string"},
                        "table_name": {"type": "string"},
                        "filter_expression": {"type": "string", "description": "RLS filter expression for the role"}
                    },
                    "required": ["role_name", "table_name", "filter_expression"]
                }
            ),
            Tool(
                name="update_table_security_role",
                description="Update an existing table security role with new filter expression or name.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "role_name": {"type": "string", "description": "The name of the existing role to update"},
                        "table_name": {"type": "string", "description": "The table name with permissions to update"},
                        "new_filter_expression": {"type": "string", "description": "New filter expression for RLS"},
                        "new_role_name": {"type": "string", "description": "New name for the role"},
                        "confirm": {"type": "boolean", "description": "Set to True to confirm the update"}
                    },
                    "required": ["role_name"]
                }
            ),
            Tool(
                name="create_lakehouse_shortcut",
                description="Create a lakehouse shortcut automatically once all required parameters are provided.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "target_workspace": {"type": "string", "description": "Name or ID of the target workspace"},
                        "target_lakehouse": {"type": "string", "description": "Name or ID of the target lakehouse"},
                        "target_shortcut_path": {"type": "string", "description": "Path in target lakehouse (e.g., 'Tables' or 'Files/folder')"},
                        "target_shortcut_name": {"type": "string", "description": "Name for the shortcut"},
                        "source_workspace": {"type": "string", "description": "Name or ID of the source workspace"},
                        "source_lakehouse": {"type": "string", "description": "Name or ID of the source lakehouse"},
                        "source_path": {"type": "string", "description": "Path in source lakehouse (e.g., 'Tables/table_name' or 'Files/folder')"}
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
            ),
            Tool(
                name="check_date_table_exists",
                description="Check if a date table exists in the model and return its details. Can check a specific table or all tables.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "Optional: specific table name to check. If not provided, checks all tables for date table candidates."
                        }
                    },
                    "required": []
                }
            ),
            Tool(
                name="mark_as_date_table",
                description="Mark a table as a date table in the model with specified date column as key.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "Name of the table to mark as date table"
                        },
                        "date_column": {
                            "type": "string",
                            "description": "Optional: specific date column to use as key. If not provided, uses the first date column found."
                        }
                    },
                    "required": ["table_name"]
                }
            ),
            Tool(
                name="unmark_date_table",
                description="Remove date table marking from a table.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "Name of the table to remove date table marking from"
                        }
                    },
                    "required": ["table_name"]
                }
            ),
            Tool(
                name="get_table_properties",
                description="Get all available properties for a specific table with their current values and metadata.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "Name of the table to inspect"
                        }
                    },
                    "required": ["table_name"]
                }
            ),
            Tool(
                name="update_table_properties",
                description="Update multiple properties of a table efficiently.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "Name of the table to update"
                        },
                        "properties": {
                            "type": "object",
                            "description": "Dictionary of property_name: value pairs to update",
                            "additionalProperties": True
                        }
                    },
                    "required": ["table_name", "properties"]
                }
            ),
            Tool(
                name="get_column_properties",
                description="Get all available properties for a specific column with their current values and metadata.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "Name of the table containing the column"
                        },
                        "column_name": {
                            "type": "string",
                            "description": "Name of the column to inspect"
                        }
                    },
                    "required": ["table_name", "column_name"]
                }
            ),
            Tool(
                name="update_column_properties",
                description="Update multiple properties of a column efficiently.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "Name of the table containing the column"
                        },
                        "column_name": {
                            "type": "string",
                            "description": "Name of the column to update"
                        },
                        "properties": {
                            "type": "object",
                            "description": "Dictionary of property_name: value pairs to update",
                            "additionalProperties": True
                        }
                    },
                    "required": ["table_name", "column_name", "properties"]
                }
            ),
            Tool(
                name="get_measure_properties",
                description="Get all available properties for a specific measure with their current values and metadata.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "Name of the table containing the measure"
                        },
                        "measure_name": {
                            "type": "string",
                            "description": "Name of the measure to inspect"
                        }
                    },
                    "required": ["table_name", "measure_name"]
                }
            ),
            Tool(
                name="update_measure_properties",
                description="Update multiple properties of a measure efficiently.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "Name of the table containing the measure"
                        },
                        "measure_name": {
                            "type": "string",
                            "description": "Name of the measure to update"
                        },
                        "properties": {
                            "type": "object",
                            "description": "Dictionary of property_name: value pairs to update",
                            "additionalProperties": True
                        }
                    },
                    "required": ["table_name", "measure_name", "properties"]
                }
            ),
            Tool(
                name="add_measure_annotations",
                description="Add custom annotations to measures for classification and metadata.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "Name of the table containing the measures"
                        },
                        "measure_name": {
                            "type": "string",
                            "description": "Name of the specific measure to annotate (optional - if not provided, applies to all measures in table)"
                        },
                        "annotations": {
                            "type": "object",
                            "description": "Dictionary of annotation_name: value pairs to add",
                            "additionalProperties": True
                        }
                    },
                    "required": ["table_name", "annotations"]
                }
            ),
            Tool(
                name="classify_all_measures_in_model",
                description="Automatically classify all measures in the entire model with intelligent annotation assignment based on DAX expression analysis.",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            ),
            Tool(
                name="analyze_dependencies",
                description="Analyze dependencies for a given object (table, column, or measure) before renaming to understand impact.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "object_type": {
                            "type": "string",
                            "description": "Type of object to analyze ('table', 'column', 'measure')"
                        },
                        "object_name": {
                            "type": "string", 
                            "description": "Name of the object to analyze"
                        },
                        "table_name": {
                            "type": "string",
                            "description": "Name of the table (required for column and measure analysis)"
                        }
                    },
                    "required": ["object_type", "object_name"]
                }
            ),
            Tool(
                name="safe_rename_with_dependencies",
                description="Safely rename an object (table, column, or measure) with dependency checking and user confirmation workflow.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "object_type": {
                            "type": "string",
                            "description": "Type of object to rename ('table', 'column', 'measure')"
                        },
                        "old_name": {
                            "type": "string",
                            "description": "Current name of the object"
                        },
                        "new_name": {
                            "type": "string",
                            "description": "New name for the object"
                        },
                        "table_name": {
                            "type": "string",
                            "description": "Name of the table (required for column and measure)"
                        },
                        "confirmed": {
                            "type": "boolean",
                            "description": "Set to True to confirm the operation after reviewing dependencies",
                            "default": False
                        }
                    },
                    "required": ["object_type", "old_name", "new_name"]
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
                elif name == "refresh_semantic_model":
                    result = await self._handle_refresh_semantic_model(arguments)
                elif name == "execute_dax_query":
                    result = await self._handle_execute_dax_query(arguments)
                elif name == "create_measure":
                    result = await self._handle_create_measure(arguments)
                elif name == "list_all_relationships":
                    result = await self._handle_list_all_relationships(arguments)  
                # REMOVED: update_column_names and update_table_name handlers - use safe_rename_with_dependencies instead
                elif name == "create_relationship":
                    result = await self._handle_create_relationship(arguments)
                elif name == "create_table_security_role":
                    result = await self._handle_create_table_security_role(arguments)
                elif name == "update_table_security_role":
                    result = await self._handle_update_table_security_role(arguments)
                elif name == "check_date_table_exists":
                    result = await self._handle_check_date_table_exists(arguments)
                elif name == "mark_as_date_table":
                    result = await self._handle_mark_as_date_table(arguments)
                elif name == "unmark_date_table":
                    result = await self._handle_unmark_date_table(arguments)
                elif name == "get_column_properties":
                    result = await self._handle_get_column_properties(arguments)
                elif name == "get_measure_properties":
                    result = await self._handle_get_measure_properties(arguments)
                elif name == "get_table_properties":
                    result = await self._handle_get_table_properties(arguments)
                elif name == "update_column_properties":
                    result = await self._handle_update_column_properties(arguments)
                elif name == "update_measure_properties":
                    result = await self._handle_update_measure_properties(arguments)
                elif name == "update_table_properties":
                    result = await self._handle_update_table_properties(arguments)
                elif name == "add_measure_annotations":
                    result = await self._handle_add_measure_annotations(arguments)
                elif name == "classify_all_measures_in_model":
                    result = await self._handle_classify_all_measures_in_model(arguments)
                elif name == "analyze_dependencies":
                    result = await self._handle_analyze_dependencies(arguments)
                elif name == "safe_rename_with_dependencies":
                    result = await self._handle_safe_rename_with_dependencies(arguments)
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

    async def _handle_create_relationship(self, arguments: Dict[str, Any]) -> str:
        """Handle creation of a new relationship"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.create_relationship,
                    arguments["from_table"],
                    arguments["from_column"],
                    arguments["to_table"],
                    arguments["to_column"],
                    arguments["is_active"],
                    arguments["cross_filter_direction"]
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

    async def _handle_get_column_properties(self, arguments: Dict[str, Any]) -> str:
        """Handle retrieval of column properties"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.get_column_properties,
                    arguments["table_name"],
                    arguments["column_name"]
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

    # REMOVED: _handle_update_table_name and _handle_update_column_names
    # These functions were redundant as safe_rename_with_dependencies provides
    # comprehensive dependency checking and safe renaming

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
    
    async def _handle_create_table_security_role(self, arguments: Dict[str, Any]) -> str:
        """Handle creation of table security role with RLS filter"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.create_table_security_role,
                    arguments["role_name"],
                    arguments["table_name"],
                    arguments["filter_expression"]
                )
                return str(result)

        except Exception as e:
            logger.error(f"Error creating table security role: {str(e)}")
            return f"Error creating table security role: {str(e)}"
    
    async def _handle_update_table_security_role(self, arguments: Dict[str, Any]) -> str:
        """Handle updating of table security role"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.update_table_security_role,
                    arguments["role_name"],
                    arguments.get("table_name"),
                    arguments.get("new_filter_expression"),
                    arguments.get("new_role_name"),
                    arguments.get("confirm", False)
                )
                return str(result)

        except Exception as e:
            logger.error(f"Error updating table security role: {str(e)}")
            return f"Error updating table security role: {str(e)}"

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

    async def _handle_create_lakehouse_shortcut(self, arguments: Dict[str, Any]) -> str:
        """Handle creation of lakehouse shortcut with approval elicitation"""
        try:
            with self.connection_lock:
                # Call the unified method with all parameters directly since it's already async
                result = await self.fabric.create_lakehouse_shortcut(
                    arguments.get("target_workspace"),
                    arguments.get("target_lakehouse"),
                    arguments.get("target_shortcut_path"),
                    arguments.get("target_shortcut_name"),
                    arguments.get("source_workspace"),
                    arguments.get("source_lakehouse"),
                    arguments.get("source_path")
                )
                
                return json.dumps(result)

        except Exception as e:
            logger.error(f"Error creating shortcut: {str(e)}")
            return f"Error creating shortcut: {str(e)}"

    async def _handle_check_date_table_exists(self, arguments: Dict[str, Any]) -> str:
        """Handle checking if date table exists in the model"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.check_date_table_exists,
                    arguments.get("table_name")
                )
                return json.dumps(result)

        except Exception as e:
            logger.error(f"Error checking date table: {str(e)}")
            return f"Error checking date table: {str(e)}"

    async def _handle_mark_as_date_table(self, arguments: Dict[str, Any]) -> str:
        """Handle marking a table as date table"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.mark_as_date_table,
                    arguments["table_name"],
                    arguments.get("date_column")
                )
                return str(result)

        except Exception as e:
            logger.error(f"Error marking table as date table: {str(e)}")
            return f"Error marking table as date table: {str(e)}"

    async def _handle_unmark_date_table(self, arguments: Dict[str, Any]) -> str:
        """Handle removing date table marking from a table"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.unmark_date_table,
                    arguments["table_name"]
                )
                return str(result)

        except Exception as e:
            logger.error(f"Error unmarking date table: {str(e)}")
            return f"Error unmarking date table: {str(e)}"
    
    async def _handle_get_table_properties(self, arguments: Dict[str, Any]) -> str:
        """Handle retrieval of table properties"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.get_table_properties,
                    arguments["table_name"]
                )
                return json.dumps(result)
                
        except Exception as e:
            logger.error(f"Error getting table properties: {str(e)}")
            return f"Error getting table properties: {str(e)}"
    
    async def _handle_update_table_properties(self, arguments: Dict[str, Any]) -> str:
        """Handle updating table properties"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.update_table_properties,
                    arguments["table_name"],
                    arguments["properties"]
                )
                return json.dumps(result)
                
        except Exception as e:
            logger.error(f"Error updating table properties: {str(e)}")
            return f"Error updating table properties: {str(e)}"
    
    async def _handle_get_measure_properties(self, arguments: Dict[str, Any]) -> str:
        """Handle retrieval of measure properties"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.get_measure_properties,
                    arguments["table_name"],
                    arguments["measure_name"]
                )
                return json.dumps(result)
                
        except Exception as e:
            logger.error(f"Error getting measure properties: {str(e)}")
            return f"Error getting measure properties: {str(e)}"
    
    async def _handle_update_measure_properties(self, arguments: Dict[str, Any]) -> str:
        """Handle updating measure properties"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.update_measure_properties,
                    arguments["table_name"],
                    arguments["measure_name"],
                    arguments["properties"]
                )
                return json.dumps(result)
                
        except Exception as e:
            logger.error(f"Error updating measure properties: {str(e)}")
            return f"Error updating measure properties: {str(e)}"
    
    async def _handle_update_column_properties(self, arguments: Dict[str, Any]) -> str:
        """Handle updating column properties"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.update_column_properties,
                    arguments["table_name"],
                    arguments["column_name"],
                    arguments["properties"]
                )
                return json.dumps(result)
                
        except Exception as e:
            logger.error(f"Error updating column properties: {str(e)}")
            return f"Error updating column properties: {str(e)}"
    
    async def _handle_add_measure_annotations(self, arguments: Dict[str, Any]) -> str:
        """Handle adding annotations to measures"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.add_measure_annotations,
                    arguments["table_name"],
                    arguments.get("measure_name"),
                    arguments["annotations"]
                )
                return json.dumps(result)
                
        except Exception as e:
            logger.error(f"Error adding measure annotations: {str(e)}")
            return f"Error adding measure annotations: {str(e)}"
    
    async def _handle_classify_all_measures_in_model(self, arguments: Dict[str, Any]) -> str:
        """Handle automatic classification of all measures in the model"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.classify_all_measures_in_model
                )
                return json.dumps(result)
                
        except Exception as e:
            logger.error(f"Error classifying all measures: {str(e)}")
            return f"Error classifying all measures: {str(e)}"
    
    async def _handle_analyze_dependencies(self, arguments: Dict[str, Any]) -> str:
        """Handle dependency analysis for an object before renaming"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.analyze_dependencies,
                    arguments["object_type"],
                    arguments["object_name"],
                    arguments.get("table_name")
                )
                return json.dumps(result, indent=2)
                
        except Exception as e:
            logger.error(f"Error analyzing dependencies: {str(e)}")
            return f"Error analyzing dependencies: {str(e)}"
    
    async def _handle_safe_rename_with_dependencies(self, arguments: Dict[str, Any]) -> str:
        """Handle safe renaming with dependency checking and user confirmation"""
        try:
            with self.connection_lock:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.tabular_editor.safe_rename_with_dependencies,
                    arguments["object_type"],
                    arguments["old_name"],
                    arguments["new_name"],
                    arguments.get("table_name"),
                    arguments.get("confirmed", False)
                )
                return json.dumps(result, indent=2)
                
        except Exception as e:
            logger.error(f"Error in safe rename operation: {str(e)}")
            return f"Error in safe rename operation: {str(e)}"
    
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