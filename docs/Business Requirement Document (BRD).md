### Lakehouse Creation
- Create  BilledPipelineLH lakehouse in OCT-Dev workspace.

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
- Create relationship between the fact and dimensions and also identify the security tables in the model and mssales access needs to be applied i.e., if a user logins in then only his/her subsidiaries should be filtered


