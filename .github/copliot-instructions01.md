### 1. 


- Use the established MCP server pattern
- Analyse the doc\modelcreation.md file
- perform the actions mentioned in the modelcreation.md file
- validate dax expression before creating in the model
- for rls tables expression cosnider proper dax expression
- whenever there is request for create relationships first list the relationships present in the model,then scan the tables and create required relationships
- ensure all created relationships are based on the existing model structure and adhere to best practices

- create a lakehouse MCPDemo_Rani in the workspace ID :07df884c-5185-45e4-9c8e-ebee3aca6605
- create 2 shortcut tables in the same workspace and consider lakehouseid of new lakehouse
Shortcut details as below:
Shortcut_Name:sec_usersubsidiary,dimsalesdate
target_workspace_id:d8bda4ab-adea-4e03-917d-157d82936d73,
target_lakehouse_id:56a634be-4eaa-4aa2-a631-36b2fa248c0d,
target_path:1.Files/CZDL/Sales/SalesExecution/BilledPipeline/v1/ext_sec_usersubsidiary, 2.Files/CZDL/Sales/SalesExecution/BilledPipeline/v1/ext_dimsalesdate
- connect to power bi dataset server:powerbi://api.powerbi.com/v1.0/myorg/OCT-Dev and database:MCP_BP_Pavan_Test
- create required relationships between facts and dimensions
- create a role seller on seller table with expression that show rows in the Seller table where the seller's email belongs to the current user's list of allowed emails from Sec_sellerhierarchy
- create a measure Total Revenue that Add up all the values in the extendedamountusd column from the Pipeline table.