"""
Complete Property Management Demo for Tabular Model
==================================================

This demonstrates the full property management capabilities for tables, columns, and measures
in your Tabular Editor MCP server.
"""

import json

# Example usage of all property management methods

def demo_table_properties():
    """Demonstrate table property inspection and updates"""
    print("=== TABLE PROPERTY MANAGEMENT ===")
    print()
    
    print("1. INSPECT TABLE PROPERTIES:")
    print("   Call: get_table_properties")
    print("   Arguments: {'table_name': 'Sales'}")
    print("   Returns:")
    
    example_table_result = {
        'table_info': {
            'table_name': 'Sales',
            'table_type': 'Table',
            'column_count': 8,
            'measure_count': 5
        },
        'editable_properties': [
            'Name', 'Description', 'IsHidden', 'IsPrivate', 'DataCategory', 
            'DisplayFolder', 'DisplayOrdinal', 'DataView', 'LineageTag', 
            'SourceLineageTag', 'Annotations', 'ExtendedProperties', 
            'ShowAsVariationsOnly'
        ],
        'readonly_properties': [
            'Source', 'Mode', 'State', 'ModifiedTime', 'RefreshedTime',
            'StructureModifiedTime', 'Columns', 'Measures', 'Partitions',
            'Hierarchies', 'DependsOn', 'ReferencedBy', 'RequestId'
        ],
        'current_values': {
            'Name': 'Sales',
            'Description': None,
            'IsHidden': False,
            'DataCategory': None,
            'DisplayFolder': None,
            'Mode': 'DirectLake',
            'Source': 'Reference: EntityPartitionSource'
        }
    }
    print(json.dumps(example_table_result, indent=2))
    print()
    
    print("2. UPDATE TABLE PROPERTIES:")
    print("   Call: update_table_properties")
    print("   Arguments:")
    update_args = {
        'table_name': 'Sales',
        'properties': {
            'Description': 'Main sales transaction table',
            'DisplayFolder': 'Core Tables',
            'DataCategory': 'Sales'
        }
    }
    print(json.dumps(update_args, indent=2))
    print("   Returns:")
    print({
        'Description': "✅ Updated from 'None' to 'Main sales transaction table'",
        'DisplayFolder': "✅ Updated from 'None' to 'Core Tables'",
        'DataCategory': "✅ Updated from 'None' to 'Sales'"
    })

def demo_column_properties():
    """Demonstrate column property inspection and updates"""
    print("\n=== COLUMN PROPERTY MANAGEMENT ===")
    print()
    
    print("1. INSPECT COLUMN PROPERTIES:")
    print("   Call: get_column_properties")
    print("   Arguments: {'table_name': 'Sales', 'column_name': 'OrderDate'}")
    print("   Returns:")
    
    example_column_result = {
        'column_info': {
            'table_name': 'Sales',
            'column_name': 'OrderDate',
            'column_type': 'DataColumn'
        },
        'editable_properties': [
            'Name', 'Description', 'DataType', 'SourceColumn', 'IsHidden',
            'IsKey', 'IsUnique', 'FormatString', 'DisplayFolder', 'DataCategory',
            'SortByColumn', 'DisplayOrdinal', 'SummarizeBy', 'EncodingHint'
        ],
        'readonly_properties': [
            'IsCalculated', 'State', 'ModifiedTime', 'RefreshedTime'
        ],
        'current_values': {
            'Name': 'OrderDate',
            'Description': None,
            'DataType': 'DateTime',
            'IsHidden': False,
            'FormatString': None,
            'DataCategory': None
        }
    }
    print(json.dumps(example_column_result, indent=2))
    print()
    
    print("2. UPDATE COLUMN PROPERTIES:")
    print("   Call: update_column_properties")
    print("   Arguments:")
    update_args = {
        'table_name': 'Sales',
        'column_name': 'OrderDate',
        'properties': {
            'Description': 'Date when the order was placed',
            'FormatString': 'mm/dd/yyyy',
            'DisplayFolder': 'Date Columns',
            'DataCategory': 'Time'
        }
    }
    print(json.dumps(update_args, indent=2))

def demo_measure_properties():
    """Demonstrate measure property inspection and updates"""
    print("\n=== MEASURE PROPERTY MANAGEMENT ===")
    print()
    
    print("1. INSPECT MEASURE PROPERTIES:")
    print("   Call: get_measure_properties")
    print("   Arguments: {'table_name': 'Sales', 'measure_name': 'Total Sales'}")
    print("   Returns:")
    
    example_measure_result = {
        'measure_info': {
            'table_name': 'Sales',
            'measure_name': 'Total Sales',
            'measure_type': 'Measure'
        },
        'editable_properties': [
            'Name', 'Description', 'Expression', 'IsHidden', 'FormatString',
            'DisplayFolder', 'DisplayOrdinal', 'KPI', 'DataCategory',
            'LineageTag', 'SourceLineageTag', 'Annotations', 'ExtendedProperties'
        ],
        'readonly_properties': [
            'IsSimpleMeasure', 'State', 'ModifiedTime', 'RefreshedTime',
            'DependsOn', 'ReferencedBy'
        ],
        'current_values': {
            'Name': 'Total Sales',
            'Description': '',
            'Expression': 'SUM(Sales[Amount])',
            'IsHidden': False,
            'FormatString': None,
            'DisplayFolder': None
        }
    }
    print(json.dumps(example_measure_result, indent=2))
    print()
    
    print("2. UPDATE MEASURE PROPERTIES:")
    print("   Call: update_measure_properties")
    print("   Arguments:")
    update_args = {
        'table_name': 'Sales',
        'measure_name': 'Total Sales',
        'properties': {
            'Description': 'Total sales amount in USD',
            'FormatString': '$#,0.00',
            'DisplayFolder': 'Financial Metrics',
            'DataCategory': 'Financial'
        }
    }
    print(json.dumps(update_args, indent=2))

def demo_batch_property_management():
    """Demonstrate batch property management scenarios"""
    print("\n=== BATCH PROPERTY MANAGEMENT SCENARIOS ===")
    print()
    
    print("SCENARIO 1: Organize Date Columns")
    print("Update multiple date columns with consistent formatting:")
    
    date_columns = ['OrderDate', 'ShipDate', 'DueDate']
    date_properties = {
        'FormatString': 'mm/dd/yyyy',
        'DisplayFolder': 'Date Columns',
        'DataCategory': 'Time',
        'Description': 'Date column formatted for display'
    }
    
    for column in date_columns:
        print(f"   update_column_properties('Sales', '{column}', {date_properties})")
    print()
    
    print("SCENARIO 2: Format Financial Measures")
    print("Update financial measures with currency formatting:")
    
    financial_measures = ['Total Sales', 'Total Cost', 'Profit']
    financial_properties = {
        'FormatString': '$#,0.00',
        'DisplayFolder': 'Financial Metrics',
        'DataCategory': 'Financial'
    }
    
    for measure in financial_measures:
        print(f"   update_measure_properties('Sales', '{measure}', {financial_properties})")
    print()
    
    print("SCENARIO 3: Hide Technical Columns")
    print("Hide ID columns and technical fields:")
    
    technical_columns = ['CustomerID', 'ProductID', 'OrderID']
    hide_properties = {
        'IsHidden': True,
        'Description': 'Internal identifier - hidden from end users'
    }
    
    for column in technical_columns:
        print(f"   update_column_properties('Sales', '{column}', {hide_properties})")
    print()
    
    print("SCENARIO 4: Organize Tables")
    print("Group related tables in display folders:")
    
    table_organization = {
        'Sales': {'DisplayFolder': 'Core Tables', 'Description': 'Main sales transaction table'},
        'Customers': {'DisplayFolder': 'Core Tables', 'Description': 'Customer master data'},
        'Products': {'DisplayFolder': 'Core Tables', 'Description': 'Product catalog'},
        'Calendar': {'DisplayFolder': 'Lookup Tables', 'Description': 'Date dimension table'}
    }
    
    for table_name, properties in table_organization.items():
        print(f"   update_table_properties('{table_name}', {properties})")

def demo_property_reference():
    """Reference guide for common properties"""
    print("\n=== PROPERTY REFERENCE GUIDE ===")
    print()
    
    print("TABLE PROPERTIES:")
    table_props = {
        'Name': 'Table name',
        'Description': 'Table description',
        'IsHidden': 'Hide from client tools (True/False)',
        'DisplayFolder': 'Organization folder name',
        'DataCategory': 'Data category (Sales, Time, Geography, etc.)',
        'LineageTag': 'Unique identifier for lineage tracking'
    }
    for prop, desc in table_props.items():
        print(f"   • {prop}: {desc}")
    print()
    
    print("COLUMN PROPERTIES:")
    column_props = {
        'Name': 'Column name',
        'Description': 'Column description',
        'IsHidden': 'Hide from client tools (True/False)',
        'IsKey': 'Mark as key column (True/False)',
        'FormatString': 'Display format (#,0.00, mm/dd/yyyy, 0.00%, etc.)',
        'DisplayFolder': 'Organization folder name',
        'DataCategory': 'Data category (Time, Geography, etc.)',
        'SortByColumn': 'Reference to column for sorting'
    }
    for prop, desc in column_props.items():
        print(f"   • {prop}: {desc}")
    print()
    
    print("MEASURE PROPERTIES:")
    measure_props = {
        'Name': 'Measure name',
        'Description': 'Measure description',
        'Expression': 'DAX expression',
        'IsHidden': 'Hide from client tools (True/False)',
        'FormatString': 'Display format ($#,0.00, 0.00%, #,0, etc.)',
        'DisplayFolder': 'Organization folder name',
        'DataCategory': 'Data category (Financial, etc.)'
    }
    for prop, desc in measure_props.items():
        print(f"   • {prop}: {desc}")
    print()
    
    print("COMMON FORMAT STRINGS:")
    format_strings = {
        'Currency': '$#,0.00, €#,0.00',
        'Percentage': '0.00%, 0.0%',
        'Numbers': '#,0 (integers), #,0.00 (decimals)',
        'Dates': 'mm/dd/yyyy, mmmm dd yyyy, yyyy-mm-dd',
        'Times': 'hh:mm:ss, hh:mm AM/PM'
    }
    for category, formats in format_strings.items():
        print(f"   • {category}: {formats}")

def demo_workflow():
    """Complete workflow example"""
    print("\n=== COMPLETE WORKFLOW EXAMPLE ===")
    print()
    
    print("STEP 1: Connect to dataset")
    print("   connect_dataset('MyWorkspace', 'MySalesDataset')")
    print()
    
    print("STEP 2: Inspect table structure")
    print("   get_table_properties('Sales')")
    print("   # Review table info and available properties")
    print()
    
    print("STEP 3: Organize table")
    print("   update_table_properties('Sales', {")
    print("       'Description': 'Main sales transaction table',")
    print("       'DisplayFolder': 'Core Tables'")
    print("   })")
    print()
    
    print("STEP 4: Inspect and format date columns")
    print("   get_column_properties('Sales', 'OrderDate')")
    print("   update_column_properties('Sales', 'OrderDate', {")
    print("       'Description': 'Date when order was placed',")
    print("       'FormatString': 'mm/dd/yyyy',")
    print("       'DisplayFolder': 'Date Columns',")
    print("       'DataCategory': 'Time'")
    print("   })")
    print()
    
    print("STEP 5: Inspect and format measures")
    print("   get_measure_properties('Sales', 'Total Sales')")
    print("   update_measure_properties('Sales', 'Total Sales', {")
    print("       'Description': 'Total sales amount in USD',")
    print("       'FormatString': '$#,0.00',")
    print("       'DisplayFolder': 'Financial Metrics'")
    print("   })")
    print()
    
    print("STEP 6: Hide technical columns")
    print("   update_column_properties('Sales', 'CustomerID', {")
    print("       'IsHidden': True,")
    print("       'Description': 'Internal customer identifier'")
    print("   })")
    print()
    
    print("STEP 7: Save and verify")
    print("   # Properties are automatically saved after each update")
    print("   # Use get_*_properties to verify changes")

if __name__ == "__main__":
    demo_table_properties()
    demo_column_properties()
    demo_measure_properties()
    demo_batch_property_management()
    demo_property_reference()
    demo_workflow()
    
    print("\n" + "="*60)
    print("AVAILABLE MCP TOOLS FOR PROPERTY MANAGEMENT:")
    print("="*60)
    
    tools = [
        "get_table_properties",
        "update_table_properties", 
        "get_column_properties",
        "update_column_properties",
        "get_measure_properties",
        "update_measure_properties"
    ]
    
    for tool in tools:
        print(f"• {tool}")
    
    print("\nAll tools support JSON input/output and provide detailed")
    print("property information with validation and error handling.")
