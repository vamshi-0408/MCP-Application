import os
import sys
import clr # type: ignore
import re
import asyncio
import json
from datetime import datetime, date
from decimal import Decimal
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List
from dotenv import load_dotenv # type: ignore
load_dotenv()


# Configure logging to stderr for MCP debugging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger(__name__)

adomd_paths = [
    r"C:\Program Files\Microsoft.NET\ADOMD.NET\160",
    r"C:\Program Files\Microsoft.NET\ADOMD.NET\150",
    r"C:\Program Files (x86)\Microsoft.NET\ADOMD.NET\160",
    r"C:\Program Files (x86)\Microsoft.NET\ADOMD.NET\150"
]

adomd_loaded = False
for path in adomd_paths:
    if os.path.exists(path):
        try:
            sys.path.append(path)
            clr.AddReference("Microsoft.AnalysisServices.AdomdClient")
            adomd_loaded = True
            logger.info(f"Loaded ADOMD.NET from {path}")
            break
        except Exception as e:
            logger.debug(f"Failed to load ADOMD.NET from {path}: {e}")
            continue

if not adomd_loaded:
    logger.error("Could not load ADOMD.NET library")
    raise ImportError("Could not load ADOMD.NET library. Please install SSMS or ADOMD.NET client.")

# Custom JSON encoder for Power BI data types
class PowerBIJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder to handle Power BI data types"""
    def default(self, obj):
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        elif isinstance(obj, Decimal):
            return float(obj)
        elif hasattr(obj, '__dict__'):
            return str(obj)
        return super().default(obj)

def clean_dax_query(dax_query: str) -> str:
    """Remove HTML/XML tags and other artifacts from DAX queries"""
    # Remove HTML/XML tags like <oii>, </oii>, etc.
    cleaned = re.sub(r'<[^>]+>', '', dax_query)
    # Remove any remaining angle brackets
    cleaned = cleaned.replace('<', '').replace('>', '')
    # Clean up extra whitespace
    cleaned = ' '.join(cleaned.split())
    return cleaned

def safe_json_dumps(data, indent=2):
    """Safely serialize data containing datetime and other non-JSON types"""
    return json.dumps(data, indent=indent, cls=PowerBIJSONEncoder)

from pyadomd import Pyadomd # type: ignore
from Microsoft.AnalysisServices.AdomdClient import AdomdSchemaGuid # type: ignore

class PowerBIConnector:
    def __init__(self):
        self.connection_string = None
        self.connected = False
        self.tables = []
        self.metadata = {}
        # Thread pool for async operations
        self.executor = ThreadPoolExecutor(max_workers=4)
    
    def connect(self, xmla_endpoint: str,initial_catalog: str) -> bool:
        """Establish connection to Power BI dataset using XMLA (i.e., XMLA) and initial catalog (i.e., dataset name)"""
        self.connection_string = (
            f"Provider=MSOLAP;"
            f"Data Source={xmla_endpoint};"
            f"Initial Catalog={initial_catalog};"
            f"User ID={os.getenv("User_ID")};"
            f"Password={os.getenv("Password")};"
        )
        
        try:
            # Test connection
            with Pyadomd(self.connection_string) as conn:
                self.connected = True
                logger.info(f"Connected to Power BI dataset: {initial_catalog}")
                # Don't discover tables during connection to speed up
                return True
        except Exception as e:
            self.connected = False
            logger.error(f"Connection failed: {str(e)}")
            raise Exception(f"Connection failed: {str(e)}")
        
    def discover_tables(self) -> List[str]:
        """Discover all user-facing tables in the dataset"""
        if not self.connected:
            raise Exception("Not connected to Power BI")
            
        # Return cached tables if already discovered
        if self.tables:
            return self.tables
            
        tables_list = []
        try:
            with Pyadomd(self.connection_string) as pyadomd_conn:
                adomd_connection = pyadomd_conn.conn
                tables_dataset = adomd_connection.GetSchemaDataSet(AdomdSchemaGuid.Tables, None)
                
                if tables_dataset and tables_dataset.Tables.Count > 0:
                    schema_table = tables_dataset.Tables[0]
                    for row in schema_table.Rows:
                        table_name = row["TABLE_NAME"]
                        if (not table_name.startswith("$") and 
                            not table_name.startswith("DateTableTemplate_") and 
                            not row["TABLE_SCHEMA"] == "$SYSTEM"):
                            tables_list.append(table_name)
                            
            self.tables = tables_list
            logger.info(f"Discovered {len(tables_list)} tables")
            return tables_list
        except Exception as e:
            logger.error(f"Failed to discover tables: {str(e)}")
            raise Exception(f"Failed to discover tables: {str(e)}")
    
    def get_table_schema(self, table_name: str) -> Dict[str, Any]:
        """Get schema information for a specific table"""
        if not self.connected:
            raise Exception("Not connected to Power BI")
            
        try:
            with Pyadomd(self.connection_string) as conn:
                cursor = conn.cursor()
                
                # Try to get column information
                try:
                    dax_query = f"EVALUATE TOPN(1, '{table_name}')"
                    cursor.execute(dax_query)
                    columns = [desc[0] for desc in cursor.description]
                    cursor.close()
                    
                    return {
                        "table_name": table_name,
                        "type": "data_table",
                        "columns": columns
                    }
                except:
                    # This might be a measure table
                    cursor.close()
                    return self.get_measures_for_table(table_name)
                    
        except Exception as e:
            logger.error(f"Failed to get schema for table '{table_name}': {str(e)}")
            raise Exception(f"Failed to get schema for table '{table_name}': {str(e)}")
        
    def get_measures_for_table(self, table_name: str) -> Dict[str, Any]:
        """Get measures for a measure table"""
        try:
            with Pyadomd(self.connection_string) as conn:
                # Get table ID
                id_cursor = conn.cursor()
                id_query = f"SELECT [ID] FROM $SYSTEM.TMSCHEMA_TABLES WHERE [Name] = '{table_name}'"
                id_cursor.execute(id_query)
                table_id_result = id_cursor.fetchone()
                id_cursor.close()
                
                if not table_id_result:
                    return {"table_name": table_name, "type": "unknown", "measures": []}
                
                table_id = table_id_result[0]
                
                # Get measures
                measure_cursor = conn.cursor()
                measure_query = f"SELECT [Name], [Expression] FROM $SYSTEM.TMSCHEMA_MEASURES WHERE [TableID] = {table_id} ORDER BY [Name]"
                measure_cursor.execute(measure_query)
                measures = measure_cursor.fetchall()
                measure_cursor.close()
                
                return {
                    "table_name": table_name,
                    "type": "measure_table",
                    "measures": [{"name": m[0], "dax": m[1]} for m in measures]
                }
                
        except Exception as e:
            logger.error(f"Failed to get measures for table '{table_name}': {str(e)}")
            return {"table_name": table_name, "type": "error", "error": str(e)}
    
    def execute_dax_query(self, dax_query: str) -> List[Dict[str, Any]]:
        """Execute a DAX query and return results"""
        if not self.connected:
            raise Exception("Not connected to Power BI")
            
        # Clean the DAX query
        cleaned_query = clean_dax_query(dax_query)
        logger.info(f"Executing DAX query: {cleaned_query}")
            
        try:
            with Pyadomd(self.connection_string) as conn:
                cursor = conn.cursor()
                cursor.execute(cleaned_query)
                
                headers = [desc[0] for desc in cursor.description]
                rows = cursor.fetchall()
                cursor.close()
                
                # Convert to list of dictionaries
                results = []
                for row in rows:
                    results.append(dict(zip(headers, row)))
                
                logger.info(f"Query returned {len(results)} rows")
                return results
                
        except Exception as e:
            logger.error(f"DAX query failed: {str(e)}")
            raise Exception(f"DAX query failed: {str(e)}")
        
    def get_sample_data(self, table_name: str, num_rows: int = 10) -> List[Dict[str, Any]]:
        """Get sample data from a table"""
        dax_query = f"EVALUATE TOPN({num_rows}, '{table_name}')"
        return self.execute_dax_query(dax_query)

from mcp.server.fastmcp import FastMCP # type: ignore
import threading
class PowerBIServer():
    def __init__(self):
        self.server = FastMCP("MCP-Application")
        self.register_tools()
        self.connector = PowerBIConnector()
        self.is_connected = False
        self.connection_lock = threading.Lock()
    
    def register_tools(self):
        # Bind tools to the server context
        @self.server.tool(
            name="connect_to_powerbi",
            description="Connect to Power BI dataset using XMLA endpoint and initial catalog."
        )
        async def connect_to_powerbi(xmla_endpoint: str, initial_catalog: str) -> str:
            def sync_connect():
                return self.connector.connect(xmla_endpoint, initial_catalog)

            with self.connection_lock:
                try:
                    # Run blocking connection in a thread pool
                    loop = asyncio.get_event_loop()
                    self.is_connected = await loop.run_in_executor(None, sync_connect)
                    if self.is_connected:
                        return "Connected successfully"
                    else:
                        return "Connection failed: Unknown error"
                except Exception as e:
                    return f"Connection failed: {str(e)}"
        
        @self.server.tool(
            name="list_tables",
            description="List all user-facing tables in the Power BI dataset."
        )        
        async def list_tables() -> List[str]:
            if not self.connector.connected:
                return "Not connected to Power BI. Please connect first using `connect_to_powerbi`."
            try:
                loop = asyncio.get_event_loop()
                tables = await loop.run_in_executor(None, self.connector.discover_tables)
                return tables
            except Exception as e:
                return f"Failed to list the tables: {str(e)}"
            
        @self.server.tool(
            name="get_table_info",
            description="Get schema information for a specific table in the Power BI dataset."
        )        
        async def get_table_info(table_name: str) -> Dict[str, Any]:
            if not self.connector.connected:
                return {"error": "Not connected to Power BI. Please connect first using `connect_to_powerbi`."}
            try:
                loop = asyncio.get_event_loop()
                schema = await loop.run_in_executor(None, self.connector.get_table_schema, table_name)
                return schema
            except Exception as e:
                return {"error": f"Failed to get schema for table '{table_name}': {str(e)}"}
        
        @self.server.tool(
            name="execute_dax_query",
            description="Execute a DAX query against the Power BI dataset and return results."
        )
        async def execute_dax_query(dax_query: str) -> List[Dict[str, Any]]:
            if not self.connector.connected:
                return {"error": "Not connected to Power BI. Please connect first using `connect_to_powerbi`."}
            try:
                loop = asyncio.get_event_loop()
                schema = await loop.run_in_executor(None, self.connector.execute_dax_query, dax_query)
                return schema
            except Exception as e:
                return {"error": f"Failed to execute the dax query'{dax_query}': {str(e)}"}
         
    def run(self, **kwargs):
        self.server.run(**kwargs)

if __name__ == "__main__":
    my_server = PowerBIServer()
    my_server.run()