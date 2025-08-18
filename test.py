from delta.tables import *
spark = SparkSession.builder \
    .appName("oct") \
    .getOrCreate()
path = "abfss://07df884c-5185-45e4-9c8e-ebee3aca6605@msit-onelake.dfs.fabric.microsoft.com/2a5d1cfa-70ba-438a-a5ec-d5a288b58d17/Tables/187datasetlist"
delta_table = DeltaTable.forPath(spark, path)
delta_table.optimize().executeCompaction()