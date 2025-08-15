# Safe Rename with Dependency Analysis and MCP Elicitation

This document describes the new functionality for safely renaming tables, columns, and measures in Power BI semantic models with comprehensive dependency analysis and user confirmation workflow.

## Overview

The safe rename functionality provides:

1. **Comprehensive Dependency Analysis** - Analyzes all objects that reference the item being renamed
2. **Risk Assessment** - Calculates risk levels and provides recommendations  
3. **User Confirmation Workflow** - Requires explicit user approval before making changes
4. **Automatic Dependency Updates** - Updates all dependent DAX expressions and references
5. **Detailed Impact Reporting** - Provides clear information about what will be affected

## New MCP Tools

### 1. analyze_dependencies

Analyzes dependencies for a given object before renaming to understand impact.

**Parameters:**
- `object_type` (required): Type of object ('table', 'column', 'measure')
- `object_name` (required): Name of the object to analyze
- `table_name` (optional): Name of the table (required for column and measure analysis)

**Returns:**
```json
{
  "object_info": {
    "type": "table",
    "name": "Sales", 
    "table_name": null
  },
  "dependent_measures": [...],
  "dependent_calculated_columns": [...],
  "dependent_relationships": [...],
  "dependent_table_security_roles": [...],
  "impact_summary": {
    "total_objects_affected": 5,
    "risk_level": "MEDIUM",
    "recommendations": [...]
  }
}
```

### 2. safe_rename_with_dependencies

Safely renames an object with dependency checking and user confirmation workflow.

**Parameters:**
- `object_type` (required): Type of object ('table', 'column', 'measure')
- `old_name` (required): Current name of the object
- `new_name` (required): New name for the object
- `table_name` (optional): Name of the table (required for column and measure)
- `confirmed` (optional): Set to True to confirm the operation (default: False)

**Two-Step Process:**

**Step 1 - Dependency Analysis (confirmed=False):**
```json
{
  "operation": "Rename table 'Sales' to 'SalesData'",
  "dependencies_analyzed": {...},
  "confirmation_required": true,
  "status": "pending_confirmation",
  "message": "⚠️ Please review dependencies and confirm the operation",
  "next_steps": [...]
}
```

**Step 2 - Execute Rename (confirmed=True):**
```json
{
  "operation": "Rename table 'Sales' to 'SalesData'", 
  "dependencies_analyzed": {...},
  "updates_performed": [
    "Updated measure KPIs[Total Sales]",
    "Updated measure KPIs[Sales Count]",
    "Renamed table 'Sales' to 'SalesData'"
  ],
  "status": "completed",
  "table_renamed": true,
  "refresh_triggered": true
}
```

## Dependency Analysis Details

### What Gets Analyzed

**For Tables:**
- Measures in any table that reference the table in DAX expressions
- Calculated columns that reference the table
- Relationships where the table is involved
- Table-level security roles and permissions

**For Columns:**
- Measures that reference the column (both `Table[Column]` and `[Column]` patterns)
- Calculated columns that reference the column
- Relationships that use the column
- Sort-by column relationships

**For Measures:**
- Other measures that reference this measure in DAX expressions
- Calculated columns that reference this measure

### Risk Level Assessment

- **NONE**: No dependencies found - safe to rename
- **LOW**: 1-5 dependent objects - minimal risk
- **MEDIUM**: 6-15 dependent objects - moderate risk, careful review needed
- **HIGH**: 16+ dependent objects - high risk, extensive testing recommended

### Pattern Matching

The dependency analysis uses sophisticated regex patterns to detect references:

- **Table References**: `\bTableName\s*\[` (matches `Sales[Amount]`, `Sales [Amount]`)
- **Column References**: `\bTable\s*\[\s*Column\s*\]` and `\[\s*Column\s*\]`
- **Measure References**: `\[\s*MeasureName\s*\]`

## Usage Workflow

### 1. Analyze Dependencies First (Recommended)

```python
# Analyze what would be affected by renaming
result = analyze_dependencies(
    object_type="table",
    object_name="Sales"
)

# Review the dependency analysis
print(f"Risk Level: {result['impact_summary']['risk_level']}")
print(f"Objects Affected: {result['impact_summary']['total_objects_affected']}")
```

### 2. Safe Rename with Confirmation

```python
# Step 1: Get confirmation prompt with dependencies
result = safe_rename_with_dependencies(
    object_type="table",
    old_name="Sales", 
    new_name="SalesTransactions",
    confirmed=False  # This will show dependencies and ask for confirmation
)

# Review the dependencies and decide whether to proceed

# Step 2: Execute the rename if approved
if user_approves:
    result = safe_rename_with_dependencies(
        object_type="table",
        old_name="Sales",
        new_name="SalesTransactions", 
        confirmed=True  # This will execute the rename
    )
```

## Examples

### Example 1: Rename Table with Dependencies

```python
# 1. Analyze dependencies
dependencies = analyze_dependencies(
    object_type="table",
    object_name="Customer"
)

# 2. Safe rename with confirmation
rename_result = safe_rename_with_dependencies(
    object_type="table",
    old_name="Customer",
    new_name="CustomerData",
    confirmed=False  # First call to see impact
)

# 3. After user review and approval
final_result = safe_rename_with_dependencies(
    object_type="table", 
    old_name="Customer",
    new_name="CustomerData",
    confirmed=True  # Execute the rename
)
```

### Example 2: Rename Column Safely

```python
# Rename a column with dependency checking
result = safe_rename_with_dependencies(
    object_type="column",
    old_name="CustomerName",
    new_name="CustomerFullName", 
    table_name="Customers",
    confirmed=False  # Review dependencies first
)

# If acceptable, confirm the rename
if acceptable:
    result = safe_rename_with_dependencies(
        object_type="column",
        old_name="CustomerName", 
        new_name="CustomerFullName",
        table_name="Customers",
        confirmed=True  # Execute
    )
```

### Example 3: Rename Measure

```python
# Rename a measure
result = safe_rename_with_dependencies(
    object_type="measure",
    old_name="Total Sales",
    new_name="Total Revenue",
    table_name="KPIs", 
    confirmed=False  # Review first
)

# Execute after confirmation
result = safe_rename_with_dependencies(
    object_type="measure",
    old_name="Total Sales",
    new_name="Total Revenue", 
    table_name="KPIs",
    confirmed=True  # Execute
)
```

## Implementation Details

### New Methods in TabularEditor Class

1. **`analyze_dependencies()`** - Core dependency analysis engine
2. **`safe_rename_with_dependencies()`** - Main rename orchestrator
3. **`_analyze_table_dependencies()`** - Table-specific dependency analysis
4. **`_analyze_column_dependencies()`** - Column-specific dependency analysis  
5. **`_analyze_measure_dependencies()`** - Measure-specific dependency analysis
6. **`_calculate_risk_level()`** - Risk assessment logic
7. **`_safe_rename_table()`** - Table rename with dependency updates
8. **`_safe_rename_column()`** - Column rename with dependency updates
9. **`_safe_rename_measure()`** - Measure rename with dependency updates

### Automatic Dependency Updates

When a rename is confirmed, the system automatically:

1. **Updates DAX Expressions** - Modifies all measures and calculated columns that reference the renamed object
2. **Preserves Relationships** - Relationships are automatically updated by the Analysis Services engine
3. **Maintains Referential Integrity** - Ensures all references remain valid after the rename
4. **Triggers Refresh** - Automatically refreshes the model when needed (for table/column renames)

## Best Practices

### 1. Always Analyze First
```python
# Good practice - analyze before renaming
dependencies = analyze_dependencies("table", "Sales")
if dependencies['impact_summary']['risk_level'] == 'HIGH':
    print("High risk - consider testing in development first")
```

### 2. Review Dependencies Carefully
- Check all dependent measures and calculated columns
- Verify that the rename makes sense in context
- Consider business impact of the change

### 3. Test in Development Environment
- For MEDIUM or HIGH risk renames, test in development first
- Validate all dependent objects work correctly after rename
- Check reports and dashboards that might be affected

### 4. Coordinate with Stakeholders
- For HIGH risk renames, notify all downstream users
- Plan the change during low-usage periods
- Have a rollback plan if issues arise

## Error Handling

The system provides comprehensive error handling:

- **Object Not Found**: Clear error when specified object doesn't exist
- **Name Conflicts**: Prevents renaming to existing object names
- **Connection Issues**: Handles disconnected tabular server gracefully
- **Permission Errors**: Reports when user lacks necessary permissions
- **DAX Expression Errors**: Validates DAX after updates

## Security Considerations

- Requires active connection to tabular server
- Respects existing security roles and permissions
- Maintains audit trail through detailed logging
- Validates user permissions before executing changes

## Performance Considerations

- Dependency analysis is optimized for large models
- Regex patterns are compiled for performance
- Model refresh is triggered only when necessary
- Changes are batched and committed together

This functionality provides a safe, controlled way to rename objects in Power BI semantic models while maintaining data integrity and providing clear visibility into the impact of changes.
