# MCP-Application

A Model Context Protocol (MCP) server for Power BI semantic model management with advanced measure classification and annotation capabilities.

## Features

- Connect to Power BI datasets via XMLA endpoints
- Automatic measure classification based on DAX expression analysis
- Custom annotation system for metadata management
- Comprehensive table, column, and measure property management
- SQL and DAX query execution
- Relationship management
- Date table configuration
- Row-level security (RLS) role management

## Automatic Measure Classification System

### Overview

The application includes an intelligent measure classification system that automatically analyzes DAX expressions and assigns standardized annotations to measures. This system helps maintain consistency and provides valuable metadata for reporting and analysis.

### Annotation Definitions

#### 1. Custom Classification
Categorizes measures based on their computational complexity and purpose:

- **Simple measure**: Basic aggregation functions without complex logic
  - Examples: `SUM()`, `COUNT()`, `AVERAGE()`, `MIN()`, `MAX()`, `DISTINCTCOUNT()`
  - Characteristics: Single function, direct column references, no filters or calculations

- **Calculated measure**: Measures involving mathematical operations or simple logic
  - Examples: Division operations, basic IF statements, measure-to-measure calculations
  - Characteristics: Uses `DIVIDE()`, mathematical operators (+, -, *, /), simple conditional logic

- **Time intelligence**: Measures using time-based calculations
  - Examples: Year-over-year comparisons, month-to-date calculations
  - Characteristics: Uses functions like `DATEADD()`, `SAMEPERIODLASTYEAR()`, `DATESYTD()`

- **Ratio/Percentage**: Measures calculating ratios, rates, or percentages
  - Examples: Conversion rates, performance ratios, percentage calculations
  - Characteristics: `DIVIDE()` function with ratio-indicating naming patterns

- **Complex measure**: Advanced calculations with multiple functions and logic
  - Examples: Multi-step calculations, complex filters, variable usage
  - Characteristics: Uses `CALCULATE()`, `FILTER()`, `VAR/RETURN` patterns, multiple nested functions

#### 2. Complexity Level
Indicates the technical complexity of the measure:

- **Low**: Simple, straightforward calculations
  - Score: 0 complexity points
  - Examples: Basic aggregations, simple divisions

- **Medium**: Moderate complexity with some advanced functions
  - Score: 1-2 complexity points
  - Examples: Single `CALCULATE()` or `IF()` statement, simple filters

- **High**: Advanced complexity with multiple functions
  - Score: 3+ complexity points
  - Examples: Multiple nested functions, complex variable logic, advanced filtering

#### 3. Category
Describes the functional purpose of the aggregation:

- **Basic aggregation**: Sum, average, min, max operations
- **Row count**: Count-based measures
- **Distinct count**: Unique value counting
- **Calculated**: Mathematical calculations and derivations
- **Advanced calculation**: Complex multi-step calculations
- **Time intelligence**: Time-based analytical measures
- **Performance metric**: KPIs and performance indicators

#### 4. Business Domain
Contextual classification based on business purpose (derived from measure names):

- **Sales Pipeline**: Pipeline value, deal-related measures
- **Sales Metrics**: Deal counts, sales performance indicators
- **Performance KPI**: Rates, percentages, performance ratios
- **Statistical Metric**: Averages, statistical calculations
- **Financial**: Revenue, cost, and financial measures
- **Operational**: Process and operational metrics

### Classification Algorithm

The system uses a multi-step analysis approach:

#### Step 1: DAX Expression Pattern Analysis
```python
# Simple Aggregation Detection
simple_patterns = [
    r'^SUM\s*\(',      # SUM functions
    r'^COUNT\s*\(',    # COUNT functions  
    r'^AVERAGE\s*\(',  # AVERAGE functions
    r'^DISTINCTCOUNT\s*\(',  # DISTINCT COUNT
]

# Calculated Measure Detection
calculated_patterns = [
    r'DIVIDE\s*\(',           # Division operations
    r'[\+\-\*\/]',           # Mathematical operators
    r'\[.*\]\s*[\+\-\*\/]'   # Measure references with math
]

# Time Intelligence Detection
time_patterns = [
    r'DATEADD\s*\(',
    r'SAMEPERIODLASTYEAR\s*\(',
    r'DATESYTD\s*\(',
    # ... additional time functions
]
```

#### Step 2: Complexity Scoring
```python
complexity_indicators = [
    ("CALCULATE(", 1),      # Filter context modification
    ("FILTER(", 2),         # Advanced filtering
    ("SUMX(", 2),          # Iterator functions
    ("IF(", 1),            # Conditional logic
    ("VAR ", 2),           # Variable usage
    ("RETURN", 2),         # Variable return
]
```

#### Step 3: Business Context Analysis
- Analyzes measure names for business domain keywords
- Applies contextual classification based on naming patterns
- Assigns appropriate business domain annotations

### Best Practices

#### 1. Naming Conventions
- Use descriptive, business-friendly measure names
- Include context indicators in names (e.g., "Total", "Average", "Rate")
- Avoid technical abbreviations in user-facing measures

#### 2. Classification Guidelines
- **Simple measures** should be preferred for basic aggregations
- Use **calculated measures** for business logic that requires mathematical operations
- Reserve **complex measures** for advanced analytical requirements
- Implement **time intelligence** measures using DAX time functions

#### 3. Annotation Management
- Classifications are automatically updated when measures are modified
- Manual annotations can override automatic classifications
- Use consistent annotation values across the model
- Regularly review and validate classifications

#### 4. Performance Considerations
- Simple measures generally perform better than complex ones
- Minimize the use of complex filtering in measure definitions
- Consider measure dependencies and calculation order
- Use variables for repeated calculations within measures

### Usage Examples

#### Automatic Classification of All Measures
```python
# Classify all measures in the entire model
result = mcp_classify_all_measures_in_model()
```

#### Manual Annotation Assignment
```python
# Add custom annotations to specific measures
annotations = {
    "Custom Classification": "Simple measure",
    "Complexity Level": "Low", 
    "Category": "Basic aggregation",
    "Business Domain": "Sales Pipeline"
}

result = mcp_add_measure_annotations(
    table_name="FactSales",
    measure_name="Total Revenue",
    annotations=annotations
)
```

#### Bulk Classification for Table
```python
# Classify all measures in a specific table
result = mcp_add_measure_annotations(
    table_name="FactSales",
    # No measure_name = applies to all measures in table
    annotations=None  # Uses automatic classification
)
```

### Classification Results

The system provides detailed results including:
- Number of measures processed
- Classification summary by type
- Detailed results for each measure
- Annotation count and success status
- DAX expression excerpts for reference

Example output:
```json
{
    "tables_processed": 1,
    "measures_processed": 16,
    "total_annotations_added": 64,
    "classification_summary": {
        "Simple measure": 6,
        "Calculated measure": 4,
        "Complex measure": 6
    },
    "status": "✅ Successfully classified 16 measures"
}
```

### Integration with Power BI

The annotation system integrates seamlessly with Power BI:
- Annotations are stored as metadata in the semantic model
- Classifications persist through model refreshes
- Annotations are accessible via XMLA and Analysis Services
- Can be queried using DAX or MDX for reporting purposes

### Troubleshooting

#### Common Issues
1. **Classification Accuracy**: If automatic classification seems incorrect, review DAX expression patterns
2. **Missing Annotations**: Ensure the model connection is active and permissions are sufficient
3. **Performance**: Large models may take time to process; consider table-by-table classification

#### Validation
- Use the measure properties tools to verify annotation assignments
- Cross-reference classifications with actual DAX expressions
- Test measure performance after classification to ensure accuracy

## Technical Implementation Details

### Core Classification Methods

#### `_auto_classify_measure(measure)`
Main classification engine that analyzes a measure and returns appropriate annotations.

**Logic Flow:**
1. Extract and normalize DAX expression
2. Apply pattern matching for classification type
3. Calculate complexity score
4. Determine functional category
5. Assign business domain based on naming
6. Return annotation dictionary

#### `_is_simple_aggregation(expression)`
Identifies basic aggregation functions using regex patterns:
```python
simple_patterns = [
    r'^SUM\s*\(',           # Direct SUM operations
    r'^COUNT\s*\(',         # Count operations
    r'^COUNTROWS\s*\(',     # Row counting
    r'^AVERAGE\s*\(',       # Average calculations
    r'^MIN\s*\(',           # Minimum values
    r'^MAX\s*\(',           # Maximum values
    r'^DISTINCTCOUNT\s*\(', # Unique counting
    r'^VALUES\s*\(',        # Value extraction
]
```

#### `_is_calculated_measure(expression)`
Detects mathematical and calculated operations:
```python
calculated_patterns = [
    r'DIVIDE\s*\(',                    # Division operations
    r'[\+\-\*\/]',                     # Math operators
    r'\[.*\]\s*[\+\-\*\/]\s*\[.*\]',  # Measure-to-measure math
]
```

#### `_get_complexity_level(expression)`
Scoring system for measure complexity:

| Function/Pattern | Complexity Score |
|------------------|------------------|
| `CALCULATE()`    | +1 point |
| `FILTER()`       | +2 points |
| `SUMX()`/`AVERAGEX()` | +2 points |
| `IF()`           | +1 point |
| `SWITCH()`       | +2 points |
| `VAR`/`RETURN`   | +2 points |
| `RELATED()`      | +1 point |
| `RELATEDTABLE()` | +2 points |

**Complexity Mapping:**
- 0 points = Low complexity
- 1-2 points = Medium complexity  
- 3+ points = High complexity

### API Endpoints

#### Classification Tools

| Tool Name | Description | Parameters |
|-----------|-------------|------------|
| `classify_all_measures_in_model` | Auto-classify all measures | None |
| `add_measure_annotations` | Add/update measure annotations | `table_name`, `measure_name?`, `annotations?` |

#### Property Management Tools

| Tool Name | Description | Parameters |
|-----------|-------------|------------|
| `get_measure_properties` | Get measure property details | `table_name`, `measure_name` |
| `update_measure_properties` | Update measure properties | `table_name`, `measure_name`, `properties` |
| `get_table_properties` | Get table property details | `table_name` |
| `update_table_properties` | Update table properties | `table_name`, `properties` |

### Data Structures

#### Annotation Format
```json
{
    "Custom Classification": "Simple measure | Calculated measure | Complex measure | Time intelligence | Ratio/Percentage",
    "Complexity Level": "Low | Medium | High", 
    "Category": "Basic aggregation | Row count | Distinct count | Calculated | Advanced calculation | Time intelligence | Performance metric",
    "Business Domain": "Sales Pipeline | Sales Metrics | Performance KPI | Statistical Metric | Financial | Operational"
}
```

#### Classification Results Format
```json
{
    "tables_processed": 1,
    "measures_processed": 16,
    "total_annotations_added": 64,
    "classification_summary": {
        "Simple measure": 6,
        "Calculated measure": 4,
        "Complex measure": 6
    },
    "details": {
        "table_name": {
            "measures": {
                "measure_name": {
                    "Custom Classification": "✅ Added 'Custom Classification' = 'Simple measure'",
                    "Complexity Level": "✅ Added 'Complexity Level' = 'Low'",
                    "Category": "✅ Added 'Category' = 'Basic aggregation'",
                    "Business Domain": "✅ Added 'Business Domain' = 'Sales Pipeline'",
                    "annotations_count": 4,
                    "dax_expression": "SUM(table[column])"
                }
            }
        }
    },
    "status": "✅ Successfully classified X measures"
}
```

### Extension Points

#### Custom Classification Rules
To add new classification patterns:

1. **Add Pattern Detection Method**
```python
def _is_custom_pattern(self, expression: str) -> bool:
    custom_patterns = [
        r'CUSTOMFUNCTION\s*\(',
        # Add your patterns
    ]
    for pattern in custom_patterns:
        if re.search(pattern, expression):
            return True
    return False
```

2. **Update Main Classification Logic**
```python
def _auto_classify_measure(self, measure) -> Dict[str, str]:
    # ... existing logic ...
    elif self._is_custom_pattern(expression):
        annotations["Custom Classification"] = "Custom measure type"
        annotations["Complexity Level"] = "Medium"
        annotations["Category"] = "Custom calculation"
```

#### Business Domain Extensions
Add domain-specific keywords:
```python
# In _auto_classify_measure method
domain_keywords = {
    "Finance": ["REVENUE", "COST", "PROFIT", "MARGIN"],
    "HR": ["EMPLOYEE", "HEADCOUNT", "TURNOVER"],
    "Inventory": ["STOCK", "INVENTORY", "WAREHOUSE"]
}
```

### Performance Optimization

#### Batch Processing
The system processes measures in batches to optimize performance:
- Processes all measures in a table together
- Single `SaveChanges()` call per table
- Efficient annotation existence checking

#### Memory Management  
- Minimal object creation during classification
- Reuse of compiled regex patterns
- Efficient string operations

#### Error Handling
- Graceful degradation for classification failures
- Detailed error logging for troubleshooting
- Partial success reporting for batch operations

## Installation and Setup

### Prerequisites
- Python 3.8+
- Power BI Premium or Premium Per User license
- XMLA read/write permissions
- Analysis Services libraries

### Dependencies
```bash
pip install -r requirements.txt
```

### Configuration
1. Set up Azure authentication
2. Configure workspace and dataset connections
3. Set XMLA endpoint permissions
4. Initialize MCP server

### Running the Server
```bash
python src/server.py
```

The server will start and listen for MCP protocol connections on stdin/stdout.
