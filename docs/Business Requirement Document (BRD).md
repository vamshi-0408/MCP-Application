### Lakehouse Creation
- Create BilledPipelineLH lakehouse in OCT-Dev workspace.
- Get the Lakehouse Info and Connect to the SQL endpoint and Database.

### Shortcuts Creation
- Consider the MSBilledPipelineLH lakehouse from MSXI-BilledPipeline02 workspace and get the below source tables and create shortcuts in OCT-Dev Workspace and BilledPipelineLH Lakehouse, Auto Approve it.
source tables:
ext_factsoftwarepipeline
ext_dimsalesdate
ext_dimopportunity
ext_dimpricinglevel
ext_sec_userbusiness
ext_dimbusiness
ext_dimproduct
ext_sec_sellerhierarchy
ext_sec_usersubsidiary

### Semantic Model Creation
- Create  BilledPipelineSM Semantic Model in OCT-Dev workspace using the above lakehouse & shortcuts info


### Relationships
- Provide a detailed description of all tables in the connected models.
- Check each table to determine whether it contains a unique key column or the lowest-granularity column.
- Establish the correct relationships between fact and dimension tables, and identify security-related tables in the model.
- Implement mssales row-level security so that when a user logs in, only their respective subsidiaries are visible.

# KPI's & Measures
- Create the key performance indicator measures, which can be used by the leaders and sellers.


