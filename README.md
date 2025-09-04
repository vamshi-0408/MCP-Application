# MCP Power BI Server

A comprehensive Model Context Protocol (MCP) server for Power BI semantic model management with advanced analytics, automated measure classification, and lakehouse integration.

## 🚀 Features

- **Power BI Integration**: Connect to Power BI datasets via XMLA endpoints
- **Intelligent Measure Classification**: Automatic measure classification based on DAX expression analysis
- **Semantic Model Management**: Create and manage DirectLake semantic models
- **Advanced Analytics**: Safe object renaming with dependency analysis
- **Lakehouse Integration**: Full Microsoft Fabric lakehouse support with shortcuts
- **Security Management**: Row-level security (RLS) configuration and management
- **Query Execution**: Both DAX and SQL query execution capabilities

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        MCP Power BI Server                          │
├─────────────────────────────────────────────────────────────────────┤
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────┐  │
│  │ PowerBIMCPServer│  │ TabularEditor   │  │ AuthenticationMgr   │  │
│  │ (Main Orchestr.)│  │ (Model Mgmt)    │  │ (Azure Auth)        │  │
│  └─────────────────┘  └─────────────────┘  └─────────────────────┘  │
├─────────────────────────────────────────────────────────────────────┤
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────┐  │
│  │   SQLEndpoint   │  │     Fabric      │  │    Logging &        │  │
│  │  (SQL Queries)  │  │ (Fabric APIs)   │  │    Threading        │  │
│  └─────────────────┘  └─────────────────┘  └─────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

## 🛠️ Installation

### Prerequisites

- Python 3.8+
- Power BI Premium or Premium Per User license
- Azure credentials with Power BI access
- SQL Server Analysis Services client libraries

### Setup Steps

1. **Clone the repository**
   ```bash
   git clone https://github.com/vamshi-0408/MCP-Application.git
   cd MCP-Application
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment variables**
   ```bash
   # Create .env file
   USER_ID=your-azure-user@domain.com
   PASSWORD=your-password
   Analysis_Services_path=C:\Program Files\Microsoft SQL Server\150\SDK\Assemblies\
   Adomd_DLL_Path=C:\Program Files\Microsoft SQL Server\150\SDK\Assemblies\
   ```

4. **Install Analysis Services libraries**
   - Download and install SQL Server Management Studio (SSMS) or
   - Install SQL Server Feature Pack for Analysis Services client libraries

5. **Configure Azure authentication**
   ```bash
   az login
   ```

## 🚀 Quick Start

### Basic Usage

```python
# Connect to Power BI dataset
await mcp_connect_dataset(
    workspace_identifier="MyWorkspace",
    database_name="SalesDataset"
)

# Execute DAX query
result = await mcp_execute_dax_query(
    "EVALUATE TOPN(10, Sales)"
)

# Create semantic model
await mcp_create_semantic_model(
    workspace_identifier="Analytics",
    semantic_model_name="SalesAnalytics",
    lakehouse_identifier="SalesLakehouse",
    selected_tables=["Sales", "Products", "Customers"]
)

# Auto-classify measures
await mcp_classify_all_measures_in_model()
```

### Running the Server

```bash
# Development mode
python src/server.py

# With debug logging
PYTHONPATH=. python src/server.py --log-level DEBUG
```

## 🎯 Core Capabilities

### 1. Intelligent Measure Classification

Automatically classifies measures based on DAX expressions:

- **Simple measure**: Basic aggregations (SUM, COUNT, AVERAGE)
- **Calculated measure**: Mathematical operations and simple logic
- **Time intelligence**: Time-based calculations
- **Complex measure**: Advanced calculations with multiple functions
- **Ratio/Percentage**: Ratio and percentage calculations

**Complexity levels**: Low, Medium, High
**Business domains**: Sales, Finance, Operations, etc.

### 2. Safe Object Renaming

Comprehensive dependency analysis before renaming:

```python
# Analyze dependencies
analysis = await mcp_analyze_dependencies(
    object_type="table",
    object_name="Sales"
)

# Safe rename with confirmation
result = await mcp_safe_rename_with_dependencies(
    object_type="table",
    old_name="Sales",
    new_name="SalesData",
    confirmed=True
)
```

### 3. Lakehouse Integration

Full Microsoft Fabric lakehouse support:

```python
# Create lakehouse shortcut
await mcp_create_lakehouse_shortcut(
    target_workspace="Analytics",
    target_lakehouse="DataMart",
    target_shortcut_path="Tables",
    target_shortcut_name="SalesData",
    source_workspace="DataWarehouse",
    source_lakehouse="RawData",
    source_path="Tables/sales_transactions"
)
```

### 4. Security Management

Row-level security configuration:

```python
# Create RLS role
await mcp_create_table_security_role(
    role_name="RegionalManager",
    table_name="Sales",
    filter_expression="Sales[Region] = USERNAME()"
)
```

## 🔧 Available Tools

### Core Connection
- `connect_dataset` - Connect to Power BI dataset
- `disconnect_dataset` - Disconnect from dataset
- `initialize_sql_connection` - Setup SQL endpoint

### Query Execution
- `execute_dax_query` - Execute DAX queries
- `execute_sql_query` - Execute SQL queries

### Model Management
- `create_semantic_model` - Create DirectLake semantic models
- `refresh_semantic_model` - Refresh semantic models
- `list_tables` - List model tables
- `create_relationship` - Create table relationships

### Advanced Analytics
- `classify_all_measures_in_model` - Auto-classify measures
- `analyze_dependencies` - Analyze object dependencies
- `safe_rename_with_dependencies` - Safe object renaming
- `add_measure_annotations` - Add measure metadata

### Fabric Integration
- `get_workspace_info` - Get workspace details
- `get_lakehouse_info` - Get lakehouse information
- `create_lakehouse` - Create new lakehouse
- `create_lakehouse_shortcut` - Create OneLake shortcuts

### Security & Properties
- `create_table_security_role` - Configure RLS
- `get_table_properties` - Get table properties
- `update_table_properties` - Update table properties
- `mark_as_date_table` - Configure date tables

## 📊 Advanced Features

### Measure Classification System

The system provides intelligent classification with:

- **Pattern Recognition**: Analyzes DAX expressions using regex patterns
- **Complexity Scoring**: Assigns complexity levels based on function usage
- **Business Context**: Infers business domain from measure names
- **Automated Annotation**: Adds standardized metadata to measures

### Dependency Analysis

Before renaming objects, the system:

- **Scans References**: Finds all DAX expressions referencing the object
- **Risk Assessment**: Calculates impact levels (None, Low, Medium, High)
- **Update Planning**: Plans automatic updates for dependent objects
- **User Confirmation**: Requires explicit approval for changes

### Performance Optimization

- **Connection Pooling**: Reuses connections for efficiency
- **Batch Operations**: Groups operations for better performance
- **Memory Management**: Proper resource cleanup and disposal
- **Thread Safety**: Concurrent operation support

## 🔍 Troubleshooting

### Common Issues

**Connection Problems**:
```python
# Test Azure authentication
from azure.identity import DefaultAzureCredential
credential = DefaultAzureCredential()
token = credential.get_token("https://analysis.windows.net/powerbi/api/.default")
```

**Performance Issues**:
- Check DAX expression complexity
- Review model relationships
- Monitor memory usage
- Use selective table processing

**Classification Issues**:
- Verify DAX expression patterns
- Check measure naming conventions
- Review automatic annotations

### Health Monitoring

Built-in health checks for:
- Azure authentication status
- XMLA connectivity
- SQL endpoint availability
- Fabric API access

## 📚 Documentation

For detailed implementation guides, see:
- `src/server.py` - Main server implementation
- `src/client.py` - Client usage examples
- `test.py` - Testing and validation examples

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests for new functionality
5. Submit a pull request

### Development Setup

```bash
# Setup development environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Run tests
pytest tests/

# Format code
black src/
isort src/
```

## 📄 License

This project is licensed under the MIT License. See LICENSE file for details.

## 🔗 Links

- [Power BI Documentation](https://docs.microsoft.com/en-us/power-bi/)
- [Microsoft Fabric Documentation](https://docs.microsoft.com/en-us/fabric/)
- [Analysis Services Documentation](https://docs.microsoft.com/en-us/analysis-services/)
- [Model Context Protocol](https://github.com/modelcontextprotocol)

## 📈 Version History

- **v1.2.0**: Added safe renaming with dependency analysis
- **v1.1.0**: Enhanced measure classification system
- **v1.0.1**: Improved error handling and logging
- **v1.0.0**: Initial release with core functionality

---

**Support**: For issues and questions, please use the GitHub issue tracker.
