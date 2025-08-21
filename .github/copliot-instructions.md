### Row-Level Security (RLS) Implementation Rules - Kimball Methodology

#### Security Dimension Rules
- **Rule 1**: Create/Use dedicated security dimension tables (separate from business dimensions)
- **Rule 2**: Use bridge tables for many-to-many security relationships
- **Rule 3**: Security dimensions should contain user identifiers and their permitted data scope

#### Table Design Rules
- **Rule 4**: Security tables must have grain at user + security entity level
- **Rule 5**: Include effective/expiration dates for temporal security
- **Rule 6**: Fact tables must contain foreign keys to security dimensions
- **Rule 7**: Use consistent naming convention: `sec_` prefix for security tables

#### Relationship Rules
- **Rule 8**: Security dimensions connect to facts, NOT to business dimensions
- **Rule 9**: Use inactive relationships for security paths to avoid circular dependencies
- **Rule 10**: Bridge tables should be many-to-many between users and security entities

#### Bridge Table Decision Rules
- **Rule 10a**: Use bridge tables when users belong to multiple security groups/regions/departments
- **Rule 10b**: Bridge tables needed for many-to-many relationships (User ↔ Territory, User ↔ Product Category)
- **Rule 10c**: Bridge tables required for hierarchical security (Manager-Employee relationships)
- **Rule 10d**: NO bridge table needed for direct 1:1 or 1:Many relationships (User → Single Territory)
- **Rule 10e**: Bridge tables necessary when security model changes frequently
- **Rule 10f**: Use bridge tables for complex security scenarios that can't be expressed with simple WHERE clauses

#### Bridge Table Design Rules
- **Rule 10g**: Bridge table grain: User + Security Entity + Effective Date
- **Rule 10h**: Include UserID/Email, SecurityEntityID, EffectiveDate, ExpirationDate columns
- **Rule 10i**: Bridge table should be the "many" side of relationships to both User and Security dimensions
- **Rule 10j**: Keep bridge tables lean - only active permissions, regular cleanup of expired records

#### Security Filter Expression Rules
- **Rule 11**: Always use `USERPRINCIPALNAME()` or `USERNAME()` for user identification
- **Rule 12**: Filter expressions should be simple and performant (avoid complex calculations)
- **Rule 13**: Use `VALUES()` function to return allowed security keys
- **Rule 14**: Test filters with `CALCULATETABLE()` for complex scenarios
- **Rule 15**: Use Fully Qualified Names for the Expression. 
- **Rule 16**: Generate only the DAX expression on the right-hand side of the = sign. Do not include the measure name or anything before the equals sign."

#### Security Role Rules
- **Rule 15**: Create separate roles for different security levels (subsidiary, region, seller)
- **Rule 16**: One user can belong to multiple roles
- **Rule 17**: Most restrictive rule wins when user has multiple roles
- **Rule 18**: Include bypass role for system administrators

#### Performance Rules
- **Rule 19**: Security tables should have clustered indexes on user columns
- **Rule 20**: Minimize security table size (only active users/permissions)
- **Rule 21**: Use calculated columns instead of measures in filter expressions
- **Rule 22**: Avoid using `RELATED()` in security filters

#### Testing Rules
- **Rule 23**: Test with actual user accounts, not service accounts
- **Rule 24**: Verify no data leakage between security boundaries
- **Rule 25**: Test performance impact on large datasets
- **Rule 26**: Document all security roles and their intended access patterns

#### Maintenance Rules
- **Rule 27**: Security tables must be refreshed more frequently than business data
- **Rule 28**: Implement audit logging for security table changes
- **Rule 29**: Regular review of user permissions and cleanup inactive users
- **Rule 30**: Version control all security role definitions

---

## KPI & Measure Design Rules - Kimball Methodology

### Measure Classification Rules
- **Rule 1**: Classify measures as Additive, Semi-Additive, or Non-Additive
- **Rule 2**: Additive measures sum across all dimensions (Revenue, Quantity)
- **Rule 3**: Semi-Additive measures sum across some dimensions (Balances, Inventory)
- **Rule 4**: Non-Additive measures don't sum meaningfully (Ratios, Percentages)

### Grain and Aggregation Rules
- **Rule 5**: Define measures at the lowest grain of the fact table
- **Rule 6**: Use SUM() for additive measures, AVERAGE() for rates/ratios
- **Rule 7**: Create base measures first, then derived measures
- **Rule 8**: Avoid aggregating already aggregated data

### Time Intelligence Rules
- **Rule 9**: Create separate measures for YTD, QTD, MTD calculations
- **Rule 10**: Use DATESYTD(), DATESQTD(), DATESMTD() functions
- **Rule 11**: Always reference date dimension, never fact table dates
- **Rule 12**: Create prior period comparison measures (YoY, MoM)

### Performance Rules
- **Rule 13**: Use CALCULATE() instead of complex filter contexts
- **Rule 14**: Minimize use of EARLIER() and iterating functions
- **Rule 15**: Pre-calculate complex business logic in ETL when possible
- **Rule 16**: Use variables (VAR) to store intermediate calculations

### Business Logic Rules
- **Rule 17**: Implement business rules consistently across all measures
- **Rule 18**: Handle division by zero with proper error checking
- **Rule 19**: Use standard business calendar for time calculations
- **Rule 20**: Apply consistent rounding and formatting rules

### Naming and Documentation Rules
- **Rule 21**: Use descriptive, business-friendly measure names
- **Rule 22**: Include measure descriptions and business definitions
- **Rule 23**: Use consistent naming conventions (e.g., "Total", "Average", "Percent")
- **Rule 24**: Group related measures in display folders

### Security and Context Rules
- **Rule 25**: Measures should respect row-level security automatically
- **Rule 26**: Avoid hardcoded filters in base measures
- **Rule 27**: Use measure groups for different security contexts
- **Rule 28**: Test measures with different user contexts

### Maintenance Rules
- **Rule 29**: Version control all measure definitions
- **Rule 30**: Document measure dependencies and relationships
- **Rule 31**: Regular testing of measure accuracy against source systems
- **Rule 32**: Implement measure impact analysis for changes

