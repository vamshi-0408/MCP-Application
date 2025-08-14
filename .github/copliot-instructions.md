# GitHub Copilot Instructions - MCP Power BI Pipeline Application

## 🎯 Project Overview

You are working on an **MCP (Model Context Protocol) Power BI application** that automates lakehouse creation, data pipeline management, and semantic modeling for sales analytics. This project focuses on the **BilledPipeline** data ecosystem.

## 🏗️ Core Architecture & Components

### **Primary Modules**
- **`src/server.py`** - MCP server handling Power BI operations
- **`src/client.py`** - Client interface for user interactions  
- **`quickstart.py`** - Rapid setup and demo functionality

### **Target Environment**
- **Workspace**: `OCT-Dev`
- **Lakehouse**: `BilledPipelineLH`
- **Semantic Model**: `BilledPipelineSM`

## 📊 Data Pipeline Requirements

### **Source Configuration**
```yaml
Source Workspace: MSXI-BilledPipeline02
Source Lakehouse: MSBilledPipelineLH
Target Workspace: OCT-Dev  
Target Lakehouse: BilledPipelineLH
```

### **Required Data Tables**
When implementing shortcuts and data connections, ensure these tables are available:

| Table Name | Purpose | Security Level |
|------------|---------|----------------|
| `ext_factsoftwarepipeline` | Core pipeline facts | Standard |
| `ext_dimsalesdate` | Date dimension | Standard |
| `ext_dimopportunity` | Opportunity data | Standard |
| `ext_dimpricinglevel` | Pricing levels | Standard |
| `ext_dimbusiness` | Business entities | Standard |
| `ext_dimproduct` | Product catalog | Standard |
| `ext_sec_userbusiness` | User-business mapping | **Security** |
| `ext_sec_sellerhierarchy` | Seller hierarchy | **Security** |
| `ext_sec_usersubsidiary` | User-subsidiary mapping | **Security** |

## 🔐 Security Implementation Guidelines

### **Row-Level Security (RLS)**
- Implement **mssales row-level security**
- Users should only see data from their respective subsidiaries
- Use security tables (`ext_sec_*`) for access control
- Test security with different user contexts

### **Authentication & Authorization**
- Validate Power BI service connections
- Implement proper error handling for auth failures
- Use environment-specific credentials

## 🚀 Implementation Workflow

### **Phase 1: Lakehouse Setup**
```python
# Priority: Create BilledPipelineLH in OCT-Dev workspace
1. Connect to OCT-Dev workspace
2. Create BilledPipelineLH lakehouse
3. Get lakehouse info and SQL endpoint
4. Initialize SQL connection
```

### **Phase 2: Data Shortcuts**
```python
# Priority: Create shortcuts from source tables
1. Connect to source workspace (MSXI-BilledPipeline02)
2. Identify required tables (see table list above)
3. Create shortcuts in target lakehouse
4. Auto-approve shortcut creation
5. Validate data accessibility
```

### **Phase 3: Semantic Model**
```python
# Priority: Build comprehensive semantic model
1. Create BilledPipelineSM semantic model
2. Establish fact-dimension relationships
3. Implement calculated measures and KPIs
4. Configure row-level security
5. Test data model integrity
```

## 📈 KPI & Measures Development

### **Sales Leadership Metrics**
- Pipeline value by stage
- Conversion rates by opportunity type
- Revenue forecasting accuracy
- Sales cycle duration analysis

### **Seller Performance Indicators**
- Individual pipeline health
- Quota attainment tracking
- Activity-to-outcome ratios
- Product mix performance

### **Business Intelligence Requirements**
- Subsidiary-based filtering
- Time-based trend analysis
- Product performance insights
- Opportunity progression tracking

## 🛠️ Development Best Practices

### **Code Quality Standards**
- **Type Hints**: Use Python type annotations
- **Error Handling**: Comprehensive exception management
- **Logging**: Structured logging for debugging
- **Documentation**: Clear docstrings and comments

### **Power BI Integration**
- Use official Power BI REST API endpoints
- Implement proper retry logic for API calls
- Handle rate limiting gracefully
- Cache frequently accessed metadata

### **Testing Strategy**
- **Unit Tests**: Individual function validation
- **Integration Tests**: End-to-end workflow testing
- **Security Tests**: RLS validation scenarios
- **Performance Tests**: Large dataset handling

## 🔧 Configuration Management

### **Environment Variables**
```python
# Required environment configurations
POWERBI_TENANT_ID = "your-tenant-id"
POWERBI_CLIENT_ID = "your-client-id" 
POWERBI_CLIENT_SECRET = "your-client-secret"
TARGET_WORKSPACE = "OCT-Dev"
SOURCE_WORKSPACE = "MSXI-BilledPipeline02"
```

### **Connection Strings**
- SQL endpoint connections for lakehouse access
- Power BI service authentication
- Secure credential management

## 🐛 Troubleshooting Guide

### **Common Issues & Solutions**

| Issue | Likely Cause | Solution |
|-------|--------------|----------|
| Authentication failures | Invalid credentials | Verify service principal permissions |
| Shortcut creation errors | Workspace permissions | Check cross-workspace access rights |
| RLS not working | Incorrect security mapping | Validate user-subsidiary relationships |
| Slow query performance | Missing relationships | Optimize data model structure |

### **Debugging Tools**
- Enable verbose logging for API interactions
- Use Power BI REST API testing tools
- Validate data lineage and relationships
- Monitor refresh operation status

## 📋 Code Review Checklist

### **Before Submitting Code**
- [ ] All required tables are accessible via shortcuts
- [ ] Row-level security is properly implemented
- [ ] KPIs and measures are accurately calculated
- [ ] Error handling covers edge cases
- [ ] Documentation is updated
- [ ] Tests pass and cover new functionality
- [ ] Performance meets requirements

### **Security Validation**
- [ ] User can only access authorized subsidiary data
- [ ] Security tables are properly joined
- [ ] Test with multiple user contexts
- [ ] Audit logs capture access patterns

---

## 💡 Key Reminders

- **Always validate data model relationships** before creating measures
- **Test security implementation** with different user roles
- **Optimize for performance** when dealing with large datasets
- **Follow MCP protocol standards** for all client-server interactions
- **Document any deviations** from the original BRD requirements