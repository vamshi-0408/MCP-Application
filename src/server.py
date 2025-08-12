import asyncio
from typing import Any, Dict, List, Optional, Union
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
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

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stderr
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

class SQLEndpointMetadata:
    """Handles SQL endpoint connections and queries with shared authentication."""
    
    def __init__(self, auth_manager: AuthenticationManager):
        self.auth_manager = auth_manager
        self.engine = None
        self.sql_endpoint = None
        self.sql_database = None
        self.driver = None
        self._connection_lock = threading.Lock()
    
    def initialize_connection(self, sql_endpoint: str, sql_database: str):
        """Initialize the SQL engine with authentication and drivers."""
        with self._connection_lock:
            self.sql_endpoint = sql_endpoint
            self.sql_database = sql_database
            
            if not self.sql_endpoint or not self.sql_database:
                raise ValueError("sql_endpoint and sql_database must be provided")
            
            # Get available SQL Server drivers
            drivers = [d for d in pyodbc.drivers() if 'SQL Server' in d]
            if not drivers:
                raise Exception("No SQL Server ODBC drivers found. Please install ODBC Driver for SQL Server.")
            
            # Use the first available driver (prefer newer versions)
            self.driver = next((d for d in ['ODBC Driver 18 for SQL Server', 'ODBC Driver 17 for SQL Server'] if d in drivers), drivers[0])
            logger.info(f"Using driver: {self.driver}")
            
            # Create SQL engine with access token authentication
            self.engine = sqlalchemy.create_engine(
                "mssql+pyodbc://", 
                creator=self._create_connection
            )
            logger.info("SQL Engine initialized and authenticated successfully")
            return self
    
    def _create_connection(self):
        """Create a database connection with fresh access token."""
        # Get fresh access token for each connection
        access_token = self.auth_manager.get_access_token()
        token = access_token.encode("UTF-16-LE")
        token_struct = struct.pack(f'<I{len(token)}s', len(token), token)
        SQL_COPT_SS_ACCESS_TOKEN = 1256
        
        # Build connection string with stored driver and endpoint info
        connection_string = f"Driver={{{self.driver}}};Server={self.sql_endpoint},1433;Database={self.sql_database};Encrypt=Yes;TrustServerCertificate=No"
        
        return pyodbc.connect(
            connection_string, 
            attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct}
        )
    
    def get_access_token(self) -> str:
        """Get the access token for the connected model to use in API calls."""
        return self.auth_manager.get_access_token()
    
    def get_lakehouse_tables_from_sql(self) -> pd.DataFrame:
        """Get lakehouse tables using the pre-authenticated SQL engine."""
        if not self.engine:
            raise Exception("Connection not initialized. Call initialize_connection() first.")
        
        df = pd.read_sql_query("SELECT name as table_name FROM sys.tables", self.engine)
        logger.info(f"Retrieved {len(df)} tables from lakehouse")
        return df
    
    def execute_sql_query(self, query: str) -> pd.DataFrame:
        """Execute any SQL query using the pre-authenticated SQL engine."""
        if not self.engine:
            raise Exception("Connection not initialized for sql server. please provide the sqlendpoint and database details")
        
        logger.info(f"Executing SQL query: {query[:100]}...")  # Log first 100 chars
        df = pd.read_sql_query(query, self.engine)
        logger.info(f"Query executed successfully, returned {len(df)} rows")
        return df
    
    def get_table_schema(self, table_name: str) -> pd.DataFrame:
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
class FabricAPIManager:
    """Handles Microsoft Fabric REST API operations with centralized authentication."""
    
    def __init__(self, auth_manager: AuthenticationManager):
        self.auth_manager = auth_manager
        self.base_url = "https://api.fabric.microsoft.com/v1"
        self._session = requests.Session()
        self._session.timeout = 30
    
    def get_fabric_access_token(self) -> str:
        """Get access token specifically for Fabric API operations."""
        # Use the existing auth manager but with Fabric-specific scope
        with self.auth_manager._lock:
            # For Fabric API, we need the powerbi scope
            token_result = self.auth_manager._credential.get_token(
                "https://analysis.windows.net/powerbi/api/.default"
            )
            return token_result.token
    
    def _make_api_request(self, method: str, endpoint: str, data: Optional[Dict] = None, 
                         use_default_credential: bool = True, access_token: str = "") -> requests.Response:
        """Make authenticated API request to Fabric REST API."""
        
        # Get access token
        if use_default_credential:
            token = self.get_fabric_access_token()
            auth_method = "DefaultAzureCredential"
        else:
            if not access_token:
                raise Exception("Access token is required when use_default_credential is False")
            token = access_token
            auth_method = "Manual access token"
        
        # Prepare headers
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        # Full URL
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        
        logger.info(f"Making {method} request to {url} using {auth_method}")
        
        # Make request
        response = self._session.request(method, url, headers=headers, json=data)
        return response
    
    def create_lakehouse_via_api(self, workspace_id: str, lakehouse_name: str, 
                                description: str = "", use_default_credential: bool = True, 
                                access_token: str = "") -> Dict[str, Any]:
        """Create a new lakehouse using Fabric REST API with Azure credential or access token authentication"""
        try:
            endpoint = f"workspaces/{workspace_id}/lakehouses"
            
            # Request body
            body = {
                "displayName": lakehouse_name,
                "description": description if description else f"Lakehouse created via MCP API - {lakehouse_name}"
            }
            
            logger.info(f"Creating lakehouse '{lakehouse_name}' in workspace '{workspace_id}'")
            
            # Make the API call
            response = self._make_api_request("POST", endpoint, body, use_default_credential, access_token)
            
            if response.status_code == 201:
                lakehouse_data = response.json()
                logger.info(f"✅ Lakehouse '{lakehouse_name}' created successfully")
                logger.info(f"Lakehouse ID: {lakehouse_data.get('id', 'N/A')}")
                return {
                    "success": True,
                    "message": f"Lakehouse '{lakehouse_name}' created successfully",
                    "lakehouse_id": lakehouse_data.get('id'),
                    "lakehouse_name": lakehouse_data.get('displayName'),
                    "workspace_id": workspace_id,
                    "description": lakehouse_data.get('description'),
                    "created_date": lakehouse_data.get('createdDate'),
                    "modified_date": lakehouse_data.get('modifiedDate'),
                    "auth_method": "DefaultAzureCredential" if use_default_credential else "Manual access token"
                }
            else:
                error_msg = f"Failed to create lakehouse. Status: {response.status_code}"
                try:
                    error_detail = response.json()
                    error_msg += f", Error: {error_detail}"
                except:
                    error_msg += f", Response: {response.text}"
                
                logger.error(error_msg)
                return {
                    "success": False,
                    "message": error_msg,
                    "status_code": response.status_code,
                    "response": response.text,
                    "auth_method": "DefaultAzureCredential" if use_default_credential else "Manual access token"
                }
                
        except requests.exceptions.RequestException as e:
            error_msg = f"Network error while creating lakehouse: {str(e)}"
            logger.error(error_msg)
            return {
                "success": False,
                "message": error_msg,
                "error_type": "NetworkError"
            }
        except Exception as e:
            error_msg = f"Unexpected error while creating lakehouse: {str(e)}"
            logger.error(error_msg)
            return {
                "success": False,
                "message": error_msg,
                "error_type": "UnexpectedError"
            }
    def create_shortcut_simple(
        self,
        workspace_id: str,
        lakehouse_id: str,
        shortcut_name: str,
        target_workspace_id: str,
        target_lakehouse_id: str,
        target_path: str = "Tables/delete_test1",
        use_default_credential: bool = True,
        access_token: str = "" 
    ) -> dict:
        """
        Simplified shortcut creation method with direct lakehouse IDs.
        
        Args:
            workspace_id: The workspace ID where the lakehouse is located
            lakehouse_id: The destination lakehouse ID
            shortcut_name: Name for the new shortcut
            target_workspace_id: Source workspace ID
            target_lakehouse_id: Source lakehouse ID
            target_path: Path in the source lakehouse (default: "Tables/delete_test1")
            use_default_credential: Whether to use DefaultAzureCredential
            access_token: Manual access token (if use_default_credential=False)
        """
        try:
            if use_default_credential:
                token = self.get_fabric_access_token()
            else:
                if not access_token:
                    raise ValueError("Access token must be provided if not using default credential.")
                token = access_token

            url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/items/{lakehouse_id}/shortcuts?shortcutConflictPolicy=Abort"

            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }

            request_body = {
                "path": "Tables",
                "name": shortcut_name,
                "target": {
                    "oneLake": {
                        "workspaceId": target_workspace_id,
                        "itemId": target_lakehouse_id,
                        "path": target_path
                    }
                }
            }

            logger.info(f"Creating shortcut '{shortcut_name}' in lakehouse '{lakehouse_id}' (workspace: {workspace_id})")
            response = requests.post(url, headers=headers, json=request_body, timeout=30)

            if response.status_code == 201:
                logger.info("✅ Shortcut created successfully.")
                return {"success": True, "message": "Shortcut created", "response": response.json()}
            else:
                logger.error(f"❌ Failed to create shortcut: {response.status_code} - {response.text}")
                return {
                    "success": False,
                    "status_code": response.status_code,
                    "message": response.text,
                    "response": response.json() if response.headers.get("content-type") == "application/json" else {}
                }

        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")
            return {"success": False, "message": str(e), "error_type": "UnexpectedError"}

    def create_semantic_model_via_api(self, workspace_id: str, semantic_model_name: str, 
                                    description: str = "", use_default_credential: bool = True, 
                                    access_token: str = "") -> Dict[str, Any]:
        """Create a new semantic model using Power BI REST API with a simple dummy table"""
        try:
            # Get access token using the same method as the working code
            if use_default_credential:
                credential = DefaultAzureCredential()
                token = credential.get_token("https://analysis.windows.net/powerbi/api/.default").token
                auth_method = "DefaultAzureCredential"
            else:
                if not access_token:
                    raise Exception("Access token is required when use_default_credential is False")
                token = access_token
                auth_method = "Manual access token"
            
            # Use Power BI REST API endpoint instead of Fabric API
            url = f'https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets'
            
            headers = {
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json'
            }
            
            # Create a simple dataset definition with a dummy table
            # This follows the Power BI API structure for creating datasets
            payload = {
                "name": semantic_model_name,
                "tables": [
                    {
                        "name": "DummyTable",
                        "columns": [
                            {
                                "name": "ID",
                                "dataType": "Int64"
                            },
                            {
                                "name": "Name", 
                                "dataType": "String"
                            },
                            {
                                "name": "Value",
                                "dataType": "Double"
                            },
                            {
                                "name": "Date",
                                "dataType": "DateTime"
                            }
                        ]
                    }
                ]
            }
            
            logger.info(f"Creating semantic model '{semantic_model_name}' in workspace '{workspace_id}' using Power BI API")
            
            # Make the API call using requests directly
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            
            if response.status_code in [200, 201, 202]:
                semantic_model_data = response.json()
                logger.info(f"✅ Semantic model '{semantic_model_name}' created successfully")
                logger.info(f"Semantic Model ID: {semantic_model_data.get('id', 'N/A')}")
                return {
                    "success": True,
                    "message": f"Semantic model '{semantic_model_name}' created successfully with dummy table",
                    "semantic_model_id": semantic_model_data.get('id'),
                    "semantic_model_name": semantic_model_data.get('name'),
                    "workspace_id": workspace_id,
                    "dummy_table": "DummyTable with columns: ID, Name, Value, Date",
                    "auth_method": auth_method,
                    "api_endpoint": "Power BI REST API"
                }
            else:
                error_msg = f"Failed to create semantic model. Status: {response.status_code}"
                try:
                    error_detail = response.json()
                    error_msg += f", Error: {error_detail}"
                except:
                    error_msg += f", Response: {response.text}"
                
                logger.error(error_msg)
                return {
                    "success": False,
                    "message": error_msg,
                    "status_code": response.status_code,
                    "response": response.text,
                    "auth_method": auth_method,
                    "api_endpoint": "Power BI REST API"
                }
                
        except requests.exceptions.RequestException as e:
            error_msg = f"Network error while creating semantic model: {str(e)}"
            logger.error(error_msg)
            return {
                "success": False,
                "message": error_msg,
                "error_type": "NetworkError"
            }
        except Exception as e:
            error_msg = f"Unexpected error while creating semantic model: {str(e)}"
            logger.error(error_msg)
            return {
                "success": False,
                "message": error_msg,
                "error_type": "UnexpectedError"
            }

class TabularEditorConnector:
    """Handles Power BI tabular model connections and operations."""
    
    def __init__(self, auth_manager: AuthenticationManager, sql_metadata: SQLEndpointMetadata):
        self.auth_manager = auth_manager
        self.sql_metadata = sql_metadata
        self.connection_string = None
        self.connected = False
        self.model = None
        self.tabularserver = TabularServer()
        self._connection_lock = threading.Lock()

    def connect(self, server_name: str, database_name: str) -> bool:
        """Connect to the powerbi server and database using the server_name and database_name parameters."""
        with self._connection_lock:
            try:
                self.connection_string = (
                    f"Provider=MSOLAP;"
                    f"Data Source={server_name};"
                    f"Initial Catalog={database_name};"
                    f"User ID={os.getenv('USER_ID')};"
                    f"Password={os.getenv('PASSWORD')};"
                )
                self.tabularserver.Connect(self.connection_string)
                self.model = self.tabularserver.Databases.FindByName(database_name).Model 
                self.connected = True
                logger.info(f"✅ Connected to model '{database_name}'")
                return True
            except Exception as e:
                logger.error(f"Connection failed: {str(e)}")
                raise

    def disconnect(self):
        """Disconnect from the powerbi server."""
        with self._connection_lock:
            if self.connected:
                self.tabularserver.Disconnect()
                self.connected = False
                logger.info("Disconnected from server.")
            return "Disconnected successfully."

    def list_tables(self) -> List[str]:
        """List all tables in the connected model."""
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        return [t.Name for t in self.model.Tables]
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
    
    def show_table_details_with_expressions(self, table_name: str) -> Dict[str, Any]:
        """Get detailed information about a table including columns and measures with expressions."""
        if not self.connected:
            raise Exception("Tabular server is not connected")
        
        table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
        if not table:
            raise Exception(f"Table '{table_name}' not found in the model.")
        
        columns_info = [
            {
                "name": col.Name,
                "data_type": str(col.DataType)
            }
            for col in table.Columns
        ]
        
        measures_info = [
            {
                "name": m.Name,
                "expression": m.Expression,
                "data_type": str(m.DataType)
            }
            for m in table.Measures
        ]
        
        logger.info(f"Table: {table.Name}")
        logger.info(f" Columns: {[col['name'] for col in columns_info]}")
        logger.info(f" Measures: {[m['name'] for m in measures_info]}")
        
        return {
            "table": table.Name,
            "columns": columns_info,
            "measures": measures_info
        }
    def hide_column(self, table_name: str, column_name: str) -> str:
        if not self.connected:
            raise Exception("Tabular server is not connected")
        table = next((t for t in self.model.Tables if t.Name == table_name), None)
        if not table:
            raise Exception(f"Table '{table_name}' not found.")
        column = next((c for c in table.Columns if c.Name == column_name), None)
        if not column:
            raise Exception(f"Column '{column_name}' not found in table '{table_name}'.")
        column.IsHidden = True
        self.model.SaveChanges()
        logger.info(f"✅ Column '{column_name}' in table '{table_name}' hidden.")
        return f"✅ Column '{column_name}' in table '{table_name}' is now hidden."

    def hide_table(self, table_name: str) -> str:
        if not self.connected:
            raise Exception("Tabular server is not connected")
        table = next((t for t in self.model.Tables if t.Name == table_name), None)
        if not table:
            raise Exception(f"Table '{table_name}' not found.")
        table.IsHidden = True
        self.model.SaveChanges()
        logger.info(f"✅ Table '{table_name}' is now hidden.")
        return f"✅ Table '{table_name}' is now hidden."

    def unhide_column(self, table_name: str, column_name: str) -> str:
        if not self.connected:
            raise Exception("Tabular server is not connected")
        table = next((t for t in self.model.Tables if t.Name == table_name), None)
        if not table:
            raise Exception(f"Table '{table_name}' not found.")
        column = next((c for c in table.Columns if c.Name == column_name), None)
        if not column:
            raise Exception(f"Column '{column_name}' not found in table '{table_name}'.")
        column.IsHidden = False
        self.model.SaveChanges()
        logger.info(f"✅ Column '{column_name}' in table '{table_name}' unhidden.")
        return f"✅ Column '{column_name}' in table '{table_name}' is now visible."

    def unhide_table(self, table_name: str) -> str:
        if not self.connected:
            raise Exception("Tabular server is not connected")
        table = next((t for t in self.model.Tables if t.Name == table_name), None)
        if not table:
            raise Exception(f"Table '{table_name}' not found.")
        table.IsHidden = False
        self.model.SaveChanges()
        logger.info(f"✅ Table '{table_name}' is now unhidden.")
        return f"✅ Table '{table_name}' is now visible."
    
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
    
    def delete_table(self, table_name: str, confirm: bool = False) -> str:
        """
        Safely delete a table from the Tabular model.
        Includes confirmation, dependency checks, and logging.

        Args:
            table_name (str): The name of the table to delete.
            confirm (bool): Whether to confirm the deletion.

        Returns:
            str: A message indicating the result of the operation.
        """
        if not self.connected:
            raise Exception("Tabular server is not connected")

        try:
            # Find table
            table = next((t for t in self.model.Tables if t.Name == table_name), None)
            if not table:
                raise Exception(f"Table '{table_name}' not found.")

            # Check for dependencies
            dependencies = []
            for tbl in self.model.Tables:
                for measure in tbl.Measures:
                    if f"{table_name}[" in measure.Expression:
                        dependencies.append(f"Measure: {measure.Name} in Table: {tbl.Name}")
                for calc_col in tbl.Columns:
                    if hasattr(calc_col, 'IsCalculated') and calc_col.IsCalculated:
                        if f"{table_name}[" in calc_col.Expression:
                            dependencies.append(f"Calculated Column: {calc_col.Name} in Table: {tbl.Name}")
            for rel in self.model.Relationships:
                if rel.FromTable.Name == table_name:
                    dependencies.append(f"Relationship From: {rel.FromTable.Name}")
                if rel.ToTable.Name == table_name:
                    dependencies.append(f"Relationship To: {rel.ToTable.Name}")

            if dependencies:
                dependency_list = "\n".join(dependencies)
                raise Exception(
                    f"Table '{table_name}' has dependencies:\n{dependency_list}\n"
                    "Please remove or update these dependencies before deleting the table."
                )

            # Confirm deletion
            if not confirm:
                return (
                    f"⚠️ Are you sure you want to delete table '{table_name}'? "
                    "Pass `confirm=True` to proceed."
                )

            # Remove the table
            self.model.Tables.Remove(table)
            self.model.SaveChanges()
            logger.info(f"Deleted table '{table_name}'.")
            return f"✅ Table '{table_name}' deleted successfully."

        except Exception as e:
            logger.error(f"Failed to delete table: {str(e)}")
            return f"❌ Failed to delete table '{table_name}': {str(e)}"
        
    def delete_column(self, table_name: str, column_name: str, confirm: bool = False) -> str:
        """
        Safely delete a column from a table in the Tabular model.
        Includes confirmation, dependency checks, and logging.

        Args:
            table_name (str): The name of the table.
            column_name (str): The name of the column to delete.
            confirm (bool): Whether to confirm the deletion.

        Returns:
            str: A message indicating the result of the operation.
        """
        if not self.connected:
            raise Exception("Tabular server is not connected")

        try:
            # Find table
            table = next((t for t in self.model.Tables if t.Name == table_name), None)
            if not table:
                raise Exception(f"Table '{table_name}' not found.")

            # Find column
            column = next((c for c in table.Columns if c.Name == column_name), None)
            if not column:
                raise Exception(f"Column '{column_name}' not found in table '{table_name}'.")

            # Check for dependencies
            dependencies = []
            for tbl in self.model.Tables:
                for measure in tbl.Measures:
                    if f"{table_name}[{column_name}]" in measure.Expression:
                        dependencies.append(f"Measure: {measure.Name} in Table: {tbl.Name}")
                for calc_col in tbl.Columns:
                    if hasattr(calc_col, 'IsCalculated') and calc_col.IsCalculated:
                        if f"{table_name}[{column_name}]" in calc_col.Expression:
                            dependencies.append(f"Calculated Column: {calc_col.Name} in Table: {tbl.Name}")
            for rel in self.model.Relationships:
                if rel.FromTable.Name == table_name and rel.FromColumn.Name == column_name:
                    dependencies.append(f"Relationship From: {rel.FromTable.Name}[{rel.FromColumn.Name}]")
                if rel.ToTable.Name == table_name and rel.ToColumn.Name == column_name:
                    dependencies.append(f"Relationship To: {rel.ToTable.Name}[{rel.ToColumn.Name}]")

            if dependencies:
                dependency_list = "\n".join(dependencies)
                raise Exception(
                    f"Column '{column_name}' has dependencies:\n{dependency_list}\n"
                    "Please remove or update these dependencies before deleting the column."
                )

            # Confirm deletion
            if not confirm:
                return (
                    f"⚠️ Are you sure you want to delete column '{column_name}' from table '{table_name}'? "
                    "Pass `confirm=True` to proceed."
                )

            # Remove the column
            table.Columns.Remove(column)
            self.model.SaveChanges()
            logger.info(f"Deleted column '{column_name}' from table '{table_name}'.")
            return f"✅ Column '{column_name}' deleted from table '{table_name}'."

        except Exception as e:
            logger.error(f"Failed to delete column: {str(e)}")
            return f"❌ Failed to delete column '{column_name}': {str(e)}"

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

    def list_table_relationships(self, table_name: str) -> Dict[str, Any]:
        """List relationships for a specific table."""
        if not self.connected:
            raise Exception("Tabular server is not connected")
            
        # Find table (case-insensitive)
        table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
        if not table:
            raise Exception(f"Table '{table_name}' not found in the model.")
            
        relationships = []
        for rel in self.model.Relationships:
            if rel.FromTable.Name == table.Name or rel.ToTable.Name == table.Name:
                rel_id = getattr(rel, 'Name', None) or getattr(rel, 'ID', None)
                relationships.append({
                    "from_table": rel.FromTable.Name,
                    "from_column": rel.FromColumn.Name,
                    "to_table": rel.ToTable.Name,
                    "to_column": rel.ToColumn.Name,
                    "relationship_id": rel_id
                })
        logger.info(f"Found {len(relationships)} relationships for table '{table.Name}'.")
        return {"relationships": relationships, "count": len(relationships)}

    def refresh_table(self, table_name: str) -> str:
        """Refresh a specific table in the model."""
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        
        table = self.model.Tables.Find(table_name)
        if table is None:
            raise Exception(f"Table '{table_name}' not found.")
        
        table.RequestRefresh(RefreshType.Full)
        self.model.SaveChanges()
        
        return f"Table '{table_name}' refresh initiated successfully."

class PowerBIMCPServer:
    """Main MCP server for Power BI operations with centralized resource management."""
    
    def __init__(self):
        self.server = Server("MCP")
        self.auth_manager = AuthenticationManager()
        self.sql_metadata = SQLEndpointMetadata(self.auth_manager)
        self.connector = TabularEditorConnector(self.auth_manager, self.sql_metadata)
        self.fabric_api = FabricAPIManager(self.auth_manager)
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.connection_lock = threading.Lock()
        self._setup_handlers()

    def _get_python_type_to_json_type(self, python_type):
        """Convert Python type annotations to JSON schema types."""
        type_mapping = {
            str: "string",
            int: "integer", 
            float: "number",
            bool: "boolean",
            list: "array",
            dict: "object"
        }
        
        # Handle typing module types
        if hasattr(python_type, '__origin__'):
            origin = python_type.__origin__
            if origin is list or origin is List:
                return "array"
            elif origin is dict or origin is Dict:
                return "object"
            elif origin is Union:  # Handle Optional[Type] which is Union[Type, None]
                # Get the non-None type from Union
                args = getattr(python_type, '__args__', ())
                non_none_types = [arg for arg in args if arg is not type(None)]
                if non_none_types:
                    return self._get_python_type_to_json_type(non_none_types[0])
        
        return type_mapping.get(python_type, "string")

    def _build_tools_from_objects(self) -> List[Tool]:
        """Build tools from both TabularEditorConnector and SQLEndpointMetadata methods."""
        tools = []
        
        # Combine methods from both objects
        objects_to_scan = [
            (self.connector, "mcp_dataset_"),
            (self.sql_metadata, "mcp_sql_"),
            (self.fabric_api, "mcp_api_")
        ]
        
        for obj, prefix in objects_to_scan:
            for method_name, method in inspect.getmembers(obj, predicate=inspect.ismethod):
                # Skip private methods and built-in methods
                if method_name.startswith('_') or method_name in ['__init__']:
                    continue
                
                # Get method signature and docstring
                sig = inspect.signature(method)
                doc = inspect.getdoc(method) or f"Execute {method_name} method"
                
                # Build input schema
                properties = {}
                required = []
                
                for param_name, param in sig.parameters.items():
                    # Skip 'self' parameter
                    if param_name == 'self':
                        continue
                    
                    # Get parameter type
                    param_type = "string"  # default
                    if param.annotation != inspect.Parameter.empty:
                        param_type = self._get_python_type_to_json_type(param.annotation)
                    
                    properties[param_name] = {"type": param_type}
                    
                    # Add to required if no default value
                    if param.default == inspect.Parameter.empty:
                        required.append(param_name)
                
                # Create tool with prefix
                tool = Tool(
                    name=f"{prefix}{method_name}",
                    description=doc,
                    inputSchema={
                        "type": "object",
                        "properties": properties,
                        "required": required
                    }
                )
                tools.append(tool)
        
        return tools

    def _setup_handlers(self):
        @self.server.list_tools()
        async def list_tools() -> List[Tool]:
            return self._build_tools_from_objects()
        
        @self.server.list_prompts()
        async def handle_list_prompts():
            return [
                "connect to Power BI using: mcp_dataset_connect(server_name, database_name)",
                "list all tables using: mcp_dataset_list_tables()",
                "add DirectLake table using: mcp_dataset_add_directlake_table(source_table, table_name)",
                "refresh specific table using: mcp_dataset_refresh_table(table_name)",
                "initialize SQL connection using: mcp_sql_initialize_connection(sql_endpoint, sql_database)",
                "query lakehouse tables using: mcp_sql_get_lakehouse_tables_from_sql()",
                "get table schema using: mcp_sql_get_table_schema(table_name)",
                "execute SQL query using: mcp_sql_execute_sql_query(query)"
            ]
    
        @self.server.call_tool()
        async def call_tool(name: str, arguments: Optional[Dict[str, Any]]) -> List[TextContent]:
            try:
                # Determine which object to use based on prefix
                if name.startswith("mcp_dataset_"):
                    obj = self.connector
                    method_name = name[12:]  # Remove "mcp_dataset_" prefix
                elif name.startswith("mcp_sql_"):
                    obj = self.sql_metadata
                    method_name = name[8:]   # Remove "mcp_sql_" prefix
                elif name.startswith("mcp_api_"):
                    obj = self.fabric_api
                    method_name = name[8:]   # Remove "mcp_api_" prefix
                else:
                    raise ValueError(f"Tool '{name}' not found")
                
                # Check if the method exists on the object
                if not hasattr(obj, method_name):
                    raise ValueError(f"Method '{method_name}' not found on object")
                
                with self.connection_lock:
                    connector_method = getattr(obj, method_name)
                    sig = inspect.signature(connector_method)
                    
                    # Prepare method arguments
                    method_args = {}
                    if arguments:
                        for param_name, param in sig.parameters.items():
                            if param_name == 'self':
                                continue
                            if param_name in arguments:
                                method_args[param_name] = arguments[param_name]
                            elif param.default == inspect.Parameter.empty:
                                raise ValueError(f"Missing required parameter: {param_name}")
                    
                    # Execute method - use executor for potentially long-running operations
                    if method_name in ['connect', 'add_directlake_table', 'refresh_table']:
                        result = await asyncio.get_event_loop().run_in_executor(
                            self.executor,
                            lambda: connector_method(**method_args)
                        )
                    else:
                        # For quick operations, run directly
                        result = connector_method(**method_args)
                    
                    formatted_result = self._format_result(result)
                    return [TextContent(type="text", text=formatted_result)]
                    
            except Exception as e:
                logger.error(f"Error executing tool '{name}': {str(e)}")
                error_msg = f"Error executing tool '{name}': {str(e)}"
                return [TextContent(type="text", text=error_msg)]

    def _format_result(self, result: Any) -> str:
        """Format the result from connector methods for MCP response."""
        if result is None:
            return "Operation completed successfully"
        elif isinstance(result, bool):
            return "True" if result else "False"
        elif isinstance(result, (str, int, float)):
            return str(result)
        elif isinstance(result, pd.DataFrame):
            return result.to_string(index=False)
        elif isinstance(result, (list, tuple)):
            if all(isinstance(item, str) for item in result):
                return "\n".join(result)
            else:
                return json.dumps(result, indent=2, default=str)
        elif isinstance(result, dict):
            return json.dumps(result, indent=2, default=str)
        else:
            return str(result)

    async def run(self):
        logger.info("Starting MCP Server...")
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="MCP",
                    server_version="1.0.0",
                    capabilities=self.server.get_capabilities(NotificationOptions(), {})
                )
            )
    
    def __del__(self):
        """Cleanup resources when server is destroyed."""
        try:
            if hasattr(self, 'executor'):
                self.executor.shutdown(wait=False)
            if hasattr(self, 'connector') and self.connector.connected:
                self.connector.disconnect()
        except:
            pass

async def main():
    await PowerBIMCPServer().run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user.")
    except Exception as e:
        logger.exception("Fatal error")
        sys.exit(1)