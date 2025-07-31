import anyio
import asyncio
from typing import Any, Dict, List, Optional
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

from Microsoft.AnalysisServices.Tabular import Server as TabularServer, RefreshType  # type: ignore
from Microsoft.AnalysisServices.AdomdClient import AdomdSchemaGuid  # type: ignore
from Microsoft.AnalysisServices.AdomdClient import AdomdConnection, AdomdCommand  # type: ignore


class TabularEditorConnector:
    def __init__(self):
        self.connection_string = None
        self.connected = False
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.model = None
        self.connection_lock = threading.Lock()
        self.tabularserver = TabularServer()

    def connect(self, server_name: str, database_name: str) -> bool:
        try:
            self.connection_string = (
                f"Provider=MSOLAP;"
                f"Data Source={server_name};"
                f"Initial Catalog={database_name};"
                f"User ID={os.getenv('User_ID')};"
                f"Password={os.getenv('Password')};"
            )
            self.tabularserver.Connect(self.connection_string)
            for db in self.tabularserver.Databases:
                if db.Name == database_name:
                    self.model = db.Model
                    self.connected = True
                    logger.info(f"✅ Connected to model '{db.Name}'")
                    return True
            raise Exception(f"❌ Database '{database_name}' not found")
        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            raise

    def disconnect(self):
        self.tabularserver.Disconnect()
        self.connected = False
        logger.info("Disconnected from server.")
        return "Disconnected successfully."

    def list_tables(self) -> List[str]:
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        return [t.Name for t in self.model.Tables]
    def evaluate_topn(self, table_name: str, top_n: int = 1) -> List[Dict[str, Any]]:
        if not self.connected:
            raise Exception("Tabular server is not connected.")
        try:
            clean_table_name = table_name.strip("'\"[]")
            if " " in clean_table_name:
                dax_expr = f'EVALUATE TOPN({top_n}, "{clean_table_name}")'
            else:
                dax_expr = f"EVALUATE TOPN({top_n}, '{clean_table_name}')"
            logger.info(f"Executing DAX: {dax_expr}")
            cmd = AdomdCommand(dax_expr, self.adomd_connection)
            reader = cmd.ExecuteReader()

            rows = []
            while reader.Read():
                row = {reader.GetName(i): reader.GetValue(i) for i in range(reader.FieldCount)}
                rows.append(row)
            logger.info(f"{len(rows)} rows returned from '{table_name}'")
            return rows
        except Exception as e:
            logger.error(f"Failed to evaluate top {top_n} rows from '{table_name}': {str(e)}")
            raise
        except Exception as e:
            logger.error(f"Failed to evaluate top {top_n} rows from '{table_name}': {str(e)}")
            raise
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
    
    def show_table_details_with_expressions(self, table_name: str) -> Dict[str, Any]:
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

    def update_measure(self, table_name: str, measure_name: str, new_name: str, new_expression: str):
        if not self.connected:
            raise Exception("Tabular server is not connected")

        # Find the table
        table = next((t for t in self.model.Tables if t.Name.lower() == table_name.lower()), None)
        if not table:
            raise Exception(f"Table '{table_name}' not found in the model.")

        # Find the measure
        measure = next((m for m in table.Measures if m.Name.lower() == measure_name.lower()), None)
        if not measure:
            raise Exception(f"Measure '{measure_name}' not found in table '{table_name}'.")

        logger.info(f"⚠️ About to update measure '{measure.Name}' in table '{table.Name}'")
        logger.info(f"Old Measure Name: {measure.Name}")
        logger.info(f"Old DAX Expression:\n{measure.Expression}")
        logger.info(f"New Measure Name: {new_name}")
        logger.info(f"New DAX Expression:\n{new_expression}")

        try:
            # Rename if needed
            if new_name and measure.Name != new_name:
                measure.Name = new_name
                logger.info(f"✅ Measure renamed to '{new_name}'.")

            # Update the expression
            measure.Expression = new_expression
            logger.info(f"✅ Measure expression updated successfully.")

            # Save changes
            self.model.SaveChanges()
            logger.info(f"✅ Measure '{measure.Name}' updated successfully.")
            return f"✅ Measure '{measure.Name}' updated successfully."

        except Exception as e:
            logger.error(f"❌ Failed to update measure '{measure_name}' in table '{table_name}': {str(e)}")
            raise

    def list_all_relationships(self) -> Dict[str, Any]:
        """List all relationships and include the count."""
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

    def count_all_relationships(self) -> int:
        """Count all relationships."""
        count = len(self.model.Relationships)
        logger.info(f"Total relationships count: {count}")
        return count

    def list_table_relationships(self, table_name: str) -> Dict[str, Any]:
        """List relationships for a specific table."""
        relationships = []
        for rel in self.model.Relationships:
            if rel.FromTable.Name == table_name or rel.ToTable.Name == table_name:
                rel_id = getattr(rel, 'Name', None) or getattr(rel, 'ID', None)
                relationships.append({
                    "from_table": rel.FromTable.Name,
                    "from_column": rel.FromColumn.Name,
                    "to_table": rel.ToTable.Name,
                    "to_column": rel.ToColumn.Name,
                    "relationship_id": rel_id
                })
        logger.info(f"Found {len(relationships)} relationships for table '{table_name}'.")
        return {"relationships": relationships, "count": len(relationships)}

    def count_table_relationships(self, table_name: str) -> int:
        """Count relationships for a specific table."""
        count = sum(1 for rel in self.model.Relationships if rel.FromTable.Name == table_name or rel.ToTable.Name == table_name)
        logger.info(f"Total relationships count for table '{table_name}': {count}")
        return count
        

class PowerBIMCPServer:
    def __init__(self):
        self.server = Server("MCP")
        self.connector = TabularEditorConnector()
        self.connection_lock = threading.Lock()
        self._setup_handlers()

    def _setup_handlers(self):
        @self.server.list_tools()
        async def list_tools() -> List[Tool]:
            return [
                Tool(
                    name="connect",
                    description="Connect to a Power BI dataset",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "server_name": {"type": "string"},
                            "database_name": {"type": "string"},
                        },
                        "required": ["server_name", "database_name"]
                    }
                ),
                Tool(
                    name="disconnect",
                    description="Disconnect from Power BI dataset",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="list_tables",
                    description="List all table names from the connected model",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                        name="evaluate_topn",
                        description="Run a TOPN query on a table to get N rows",
                        inputSchema={
                            "type": "object",
                            "properties": {
                                "table_name": {"type": "string"},
                                "top_n": {"type": "integer", "default": 1}
                            },
                            "required": ["table_name"]
                        }
                    ),
                Tool(
                                name="delete_table",
                                description="Delete a table from the Power BI dataset",
                                inputSchema={
                                    "type": "object",
                                    "properties": {
                                        "table_name": {"type": "string"},
                                        "confirm": {"type": "boolean", "default": False}
                                    },
                                    "required": ["table_name"]
                                }
                            ),
                Tool(
                        name="delete_column",
                        description="Delete a column from a table in Power BI dataset",
                        inputSchema={
                            "type": "object",
                            "properties": {
                                "table_name": {"type": "string"},
                                "column_name": {"type": "string"},
                                "confirm": {"type": "boolean", "default": False}
                            },
                            "required": ["table_name", "column_name"]
                        }
                    ),

                Tool(
                    name="execute_dax_query",
                    description="Execute a DAX query and return results",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "dax_query": {"type": "string"}
                        },
                        "required": ["dax_query"]
                    }
                ),    
                Tool(
                    name="update_column_names",
                    description="Rename a column in a table",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "table_name": {"type": "string"},
                            "old_col_name": {"type": "string"},
                            "new_col_name": {"type": "string"}
                        },
                        "required": ["table_name", "old_col_name", "new_col_name"]
                    }
                ),
                Tool(
                    name="update_table_name",
                    description="Rename table name",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "old_table_name": {"type": "string"},
                            "new_table_name": {"type": "string"},
                            "confirm": {"type": "boolean", "default": False}
                        },
                        "required": ["old_table_name", "new_table_name"]
                    }
                ),
                Tool(
    name="show_table_details_with_expressions",
    description="Get column names and measure expressions for a given table",
    inputSchema={
        "type": "object",
        "properties": {
            "table_name": {"type": "string"}
        },
        "required": ["table_name"]
    }
),
                Tool(
                    name="hide_column",
                    description="Hide a column in a table",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "table_name": {"type": "string"},
                            "column_name": {"type": "string"}
                        },
                        "required": ["table_name", "column_name"]
                    }
                ),
                Tool(
                    name="hide_table",
                    description="Hide a table",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "table_name": {"type": "string"}
                        },
                        "required": ["table_name"]
                    }
                ),
                Tool(
                    name="unhide_column",
                    description="Unhide a column in a table",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "table_name": {"type": "string"},
                            "column_name": {"type": "string"}
                        },
                        "required": ["table_name", "column_name"]
                    }
                ),
                Tool(
                    name="unhide_table",
                    description="Unhide a table",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "table_name": {"type": "string"}
                        },
                        "required": ["table_name"]
                    }
                ),
                Tool(
                    name="update_measure",
                    description="Update a measure's name and/or DAX expression in a table",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "table_name": {"type": "string"},
                            "measure_name": {"type": "string"},
                            "new_name": {"type": "string"},
                            "new_expression": {"type": "string"}
                        },
                        "required": ["table_name", "measure_name", "new_expression"]
                    }
                ),
                Tool(
                    name="list_all_relationships",
                    description="List all relationships and include the count",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="count_all_relationships",
                    description="Count all relationships",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="list_table_relationships",
                    description="List relationships for a specific table",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "table_name": {"type": "string"}
                        },
                        "required": ["table_name"]
                    }
                ),
                Tool(
                    name="count_table_relationships",
                    description="Count relationships for a specific table",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "table_name": {"type": "string"}
                        },
                        "required": ["table_name"]
                    }
                ),

            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: Optional[Dict[str, Any]]) -> List[TextContent]:
            try:
                logger.info(f"Tool call received: {name}")
                result = ""

                with self.connection_lock:
                    if name == "connect":
                        result = await asyncio.get_event_loop().run_in_executor(
                            None,
                            self.connector.connect,
                            arguments["server_name"],
                            arguments["database_name"]
                        )
                        result = "Connected successfully."

                    elif name == "disconnect":
                        result = await asyncio.get_event_loop().run_in_executor(None, self.connector.disconnect)

                    elif name == "list_tables":
                        result = json.dumps(
                            await asyncio.get_event_loop().run_in_executor(None, self.connector.list_tables),
                            indent=2
                        )
                    elif name == "evaluate_topn":
                        result = json.dumps(
                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        self.connector.evaluate_topn,
                        arguments["table_name"],
                        arguments.get("top_n", 1)
                    ),
                    indent=2
                )
                    elif name == "execute_dax_query":
                        result = json.dumps(
                            await asyncio.get_event_loop().run_in_executor(
                                None,
                                self.connector.execute_dax_query,
                                arguments["dax_query"]
                            ),
                            indent=2
                        )
                    elif name == "delete_table":
                        result = await asyncio.get_event_loop().run_in_executor(
        None,
        self.connector.delete_table,
        arguments["table_name"],
        arguments.get("confirm", False)
    )
                    elif name == "delete_column":
                        result = await asyncio.get_event_loop().run_in_executor(
                            None,
                            self.connector.delete_column,
                            arguments["table_name"],
                            arguments["column_name"],
                            arguments.get("confirm", False)
                        )

                    elif name == "update_column_names":
                        result = await asyncio.get_event_loop().run_in_executor(
                            None,
                            self.connector.update_column_names,
                            arguments["table_name"],
                            arguments["old_col_name"],
                            arguments["new_col_name"]
                        )

                    elif name == "update_table_name":
                        result = await asyncio.get_event_loop().run_in_executor(
        None,
        self.connector.update_table_name,
        arguments["old_table_name"],
        arguments["new_table_name"],
        arguments.get("confirm", False)
    )
                    elif name == "show_table_details_with_expressions":
                        result = json.dumps(
        await asyncio.get_event_loop().run_in_executor(
            None,
            self.connector.show_table_details_with_expressions,
            arguments["table_name"]
        ),
        indent=2
    )

                    elif name == "hide_column":
                        result = await asyncio.get_event_loop().run_in_executor(
        None,
        self.connector.hide_column,
        arguments["table_name"],
        arguments["column_name"]
    )
                    elif name == "hide_table":
                        result = await asyncio.get_event_loop().run_in_executor(
        None,
        self.connector.hide_table,
        arguments["table_name"]
    )
                    elif name == "unhide_column":
                        result = await asyncio.get_event_loop().run_in_executor(
        None,
        self.connector.unhide_column,
        arguments["table_name"],
        arguments["column_name"]
    )
                    elif name == "unhide_table":
                        result = await asyncio.get_event_loop().run_in_executor(
        None,
        self.connector.unhide_table,
        arguments["table_name"]
    )
                    elif name == "update_measure":
                        result = await asyncio.get_event_loop().run_in_executor(
        None,
        self.connector.update_measure,
        arguments["table_name"],
        arguments["measure_name"],
        arguments.get("new_name", None),
        arguments["new_expression"]
    )
                    elif name == "list_all_relationships":
                        result = json.dumps(
                            await asyncio.get_event_loop().run_in_executor(
                                None,
                                self.connector.list_all_relationships
                            ),
                            indent=2
                        )
                    elif name == "count_all_relationships":
                        result = await asyncio.get_event_loop().run_in_executor(
                            None,
                            self.connector.count_all_relationships
                        )
                    elif name == "list_table_relationships":
                        result = json.dumps(
                            await asyncio.get_event_loop().run_in_executor(
                                None,
                                self.connector.list_table_relationships,
                                arguments["table_name"]
                            ),
                            indent=2
                        )
                    elif name == "count_table_relationships":
                        result = await asyncio.get_event_loop().run_in_executor(
                            None,
                            self.connector.count_table_relationships,
                            arguments["table_name"]
                        )

                    else:
                        return [TextContent(type="text", text=f"Unknown tool: {name}")]

                return [TextContent(type="text", text=result)]
            except Exception as e:
                logger.exception("Tool execution error")
                return [TextContent(type="text", text=f"Error: {str(e)}")]
    async def _handle_connect(self, arguments: Dict[str, Any]) -> str:
        """Handle connection to Power BI"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.connector.connect,
                    arguments["server_name"],
                    arguments["database_name"]
                )
                return f"Successfully connected to Power BI dataset."
                
        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"
    
    async def _handle_update_column_names(self, arguments: Dict[str, Any]) -> str:
        """Handle update the column names in a table"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                msg = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.connector.update_column_names,
                    arguments["table_name"],
                    arguments["old_col_name"],
                    arguments["new_col_name"]
                )
                return msg
                
        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"
    async def update_table_name(self, arguments: Dict[str, Any]) -> str:
        """Handle update the column names in a table"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                msg = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.connector.update_table_name,
                    arguments["old_table_name"],
                    arguments["new_table_name"],
                    arguments.get("confirm", False)
                )
                return msg
                
        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"
    async def _handle_show_table_details_with_expressions(self, arguments: Dict[str, Any]) -> str:
        """Handle update the column names in a table"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                msg = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.connector.show_table_details_with_expressions,
                arguments["table_name"]
                )
                return msg
                
        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return f"Connection failed: {str(e)}"
    
    
    async def _handle_disconnect(self, arguments: Dict[str, Any]) -> str:
        """Handle connection to Power BI"""
        try:
            with self.connection_lock:
                # Connect to Power BI
                msg = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.connector.disconnect
                )
                return msg
                
        except Exception as e:
            logger.error(f"error in disconnecting the model from mcp server: {str(e)}")
            return f"error in disconnecting the model from mcp server: {str(e)}"
    async def _handle_list_tables(self) -> str:
        try:
            with self.connection_lock:
                tables = await asyncio.get_event_loop().run_in_executor(
                None,
                self.connector.list_tables
            )
            return json.dumps(tables, indent=2)
        except Exception as e:
            logger.error(f"Failed to list tables: {str(e)}")
        return f"Failed to list tables: {str(e)}"

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
