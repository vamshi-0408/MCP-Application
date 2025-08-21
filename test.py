import os
import sys
import json
import csv
import io
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime
import logging
import sys
import io
import csv
import socket

try:
    from azure.identity import AzureCliCredential
    from azure.storage.blob import BlobServiceClient
    from azure.search.documents import SearchClient
    from azure.search.documents.indexes import SearchIndexClient, SearchIndexerClient
    from azure.search.documents.indexes.models import (
        SearchIndex,
        SearchField,
        SearchFieldDataType,
        SimpleField,
        SearchableField,
        ComplexField,
        SearchIndexer,
        SearchIndexerDataContainer,
        SearchIndexerDataSourceConnection,
        IndexingSchedule,
        FieldMapping
    )
    from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
except ImportError as e:
    print(f"Missing required packages. Please install: {e}")
    print("Run: pip install azure-storage-blob azure-search-documents azure-identity")
    sys.exit(1)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class AzureSearchCSVImporter:
    """Handles importing CSV data from Azure Blob Storage to Azure AI Search"""
    
    def __init__(self, search_service_name: str, search_admin_key: Optional[str] = None, 
                 custom_endpoint: Optional[str] = None):
        """
        Initialize the importer with Azure AI Search service details
        
        Args:
            search_service_name: Name of the Azure AI Search service
            search_admin_key: Admin key for the search service (optional if using managed identity)
            custom_endpoint: Custom endpoint URL for private endpoints (optional)
        """
        self.search_service_name = search_service_name
        
        # Use custom endpoint if provided (for private endpoints), otherwise use public endpoint
        if custom_endpoint:
            self.search_endpoint = custom_endpoint
            logger.info(f"Using custom endpoint: {custom_endpoint}")
        else:
            self.search_endpoint = f"https://{search_service_name}.search.windows.net"
            logger.info(f"Using public endpoint: {self.search_endpoint}")
        
        # Use Azure CLI credentials
        self.credential = AzureCliCredential()
        
        # Initialize search clients
        if search_admin_key:
            from azure.core.credentials import AzureKeyCredential
            credential = AzureKeyCredential(search_admin_key)
        else:
            credential = self.credential
            
        self.index_client = SearchIndexClient(
            endpoint=self.search_endpoint,
            credential=credential
        )
        
        self.indexer_client = SearchIndexerClient(
            endpoint=self.search_endpoint,
            credential=credential
        )
        
    def get_blob_data(self, storage_account_name: str, container_name: str, blob_name: str) -> str:
        """
        Download CSV data from Azure Blob Storage
        
        Args:
            storage_account_name: Azure storage account name
            container_name: Container name
            blob_name: Blob (file) name
            
        Returns:
            CSV data as string
        """
        try:
            # Create blob service client using Azure CLI credentials
            account_url = f"https://{storage_account_name}.blob.core.windows.net"
            blob_service_client = BlobServiceClient(
                account_url=account_url,
                credential=self.credential
            )
            
            # Download blob content
            blob_client = blob_service_client.get_blob_client(
                container=container_name,
                blob=blob_name
            )
            
            logger.info(f"Downloading blob: {blob_name} from container: {container_name}")
            blob_data = blob_client.download_blob().readall()
            
            # Decode the data (assuming UTF-8 encoding)
            csv_data = blob_data.decode('utf-8')
            logger.info(f"Successfully downloaded {len(csv_data)} characters of CSV data")
            
            return csv_data
            
        except Exception as e:
            logger.error(f"Error downloading blob data: {str(e)}")
            raise
    
    def parse_csv_data(self, csv_data: str) -> List[Dict[str, Any]]:
        """
        Parse CSV data into a list of dictionaries
        
        Args:
            csv_data: CSV data as string
            
        Returns:
            List of dictionaries representing the CSV rows
        """
        try:
            csv_reader = csv.DictReader(io.StringIO(csv_data))
            rows = []
            
            for i, row in enumerate(csv_reader):
                # Add a unique ID for each document
                row['id'] = str(i + 1)
                # Clean up any None values
                cleaned_row = {k: (v if v is not None else '') for k, v in row.items()}
                rows.append(cleaned_row)
                # Add a unique ID for each document
                row['id'] = str(i + 1)
                # Clean up any None values
                cleaned_row = {k: (v if v is not None else '') for k, v in row.items()}
                rows.append(cleaned_row)
            
            logger.info(f"Parsed {len(rows)} rows from CSV data")
            return rows
            
        except Exception as e:
            logger.error(f"Error parsing CSV data: {str(e)}")
            raise
    
    def infer_schema_from_data(self, data: List[Dict[str, Any]], index_name: str) -> SearchIndex:
        """
        Infer search index schema from CSV data
        
        Args:
            data: List of dictionaries representing the data
            index_name: Name for the search index
            
        Returns:
            SearchIndex object
        """
        if not data:
            raise ValueError("No data provided for schema inference")
        
        # Analyze the first few rows to determine field types
        sample_rows = data[:min(10, len(data))]
        fields = []
        
        # Always add an ID field
        fields.append(SimpleField(name="id", type=SearchFieldDataType.String, key=True))
        
        # Analyze each column
        for column_name in data[0].keys():
            if column_name == 'id':
                continue  # Skip ID field as it's already added
                
            # Sample values for type inference
            sample_values = [row.get(column_name, '') for row in sample_rows if row.get(column_name)]
            
            # Determine field type based on sample values
            field_type = self._infer_field_type(sample_values)
            
            if field_type == SearchFieldDataType.String:
                # Make string fields searchable by default
                fields.append(SearchableField(
                    name=column_name,
                    type=SearchFieldDataType.String,
                    filterable=True,
                    facetable=True
                ))
            else:
                # Non-string fields
                fields.append(SimpleField(
                    name=column_name,
                    type=field_type,
                    filterable=True,
                    facetable=(field_type in [SearchFieldDataType.Int32, SearchFieldDataType.Double])
                ))
                fields.append(SimpleField(
                    name=column_name,
                    type=field_type,
                    filterable=True,
                    facetable=True
                ))
        
        # Create the search index
        index = SearchIndex(name=index_name, fields=fields)
        logger.info(f"Created index schema with {len(fields)} fields")
        
        return index
    
    def _infer_field_type(self, sample_values: List[str]) -> SearchFieldDataType:
        """
        Infer the appropriate SearchFieldDataType based on sample values
        
        Args:
            sample_values: List of sample values
            
        Returns:
            Appropriate SearchFieldDataType
        """
        if not sample_values:
            return SearchFieldDataType.String
        
        # Try to determine if all values are integers
        try:
            for value in sample_values:
                if value.strip():  # Skip empty values
                    int(value.strip())
            return SearchFieldDataType.Int32
        except ValueError:
            pass
        
        # Try to determine if all values are floats
        try:
            for value in sample_values:
                if value.strip():  # Skip empty values
                    float(value.strip())
            return SearchFieldDataType.Double
        except ValueError:
            pass
        
        # Try to determine if values are dates
        for value in sample_values:
            if value.strip():
                try:
                    # Try common date formats
                    for date_format in ['%Y-%m-%d', '%m/%d/%Y', '%d/%m/%Y', '%Y-%m-%d %H:%M:%S']:
                        try:
                            datetime.strptime(value.strip(), date_format)
                            return SearchFieldDataType.DateTimeOffset
                        except ValueError:
                            continue
                except:
                    continue
        
        # Default to string
        return SearchFieldDataType.String
    
    def create_or_update_index(self, index: SearchIndex) -> bool:
        """
        Create or update the search index
        
        Args:
            index: SearchIndex object
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Try to create the index
            result = self.index_client.create_index(index)
            logger.info(f"Created new index: {index.name}")
            return True
            
        except ResourceExistsError:
            # Index exists, update it
            logger.info(f"Index {index.name} already exists, updating...")
            try:
                result = self.index_client.create_or_update_index(index)
                logger.info(f"Updated index: {index.name}")
                return True
            except Exception as e:
                logger.error(f"Error updating index: {str(e)}")
                return False
                
        except Exception as e:
            logger.error(f"Error creating index: {str(e)}")
            return False
    
    def upload_documents(self, index_name: str, documents: List[Dict[str, Any]], batch_size: int = 1000) -> bool:
        """
        Upload documents to the search index
        
        Args:
            index_name: Name of the search index
            documents: List of documents to upload
            batch_size: Number of documents to upload in each batch
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Create search client for document operations
            search_client = SearchClient(
                endpoint=self.search_endpoint,
                index_name=index_name,
                credential=self.credential
            )
            
            # Upload documents in batches
            total_docs = len(documents)
            uploaded = 0
            
            for i in range(0, total_docs, batch_size):
                batch = documents[i:i + batch_size]
                
                try:
                    result = search_client.upload_documents(documents=batch)
                    uploaded += len(batch)
                    logger.info(f"Uploaded batch {i//batch_size + 1}: {uploaded}/{total_docs} documents")
                    
                except Exception as e:
                    logger.error(f"Error uploading batch {i//batch_size + 1}: {str(e)}")
                    return False
            
            logger.info(f"Successfully uploaded {uploaded} documents to index {index_name}")
            return True
            
        except Exception as e:
            logger.error(f"Error uploading documents: {str(e)}")
            return False
    
    def test_connectivity(self) -> bool:
        """
        Test connectivity to the Azure AI Search endpoint
        
        Returns:
            True if connection is successful, False otherwise
        """
        try:
            logger.info("Testing connectivity to Azure AI Search endpoint...")
            
            # For private endpoints, skip DNS and socket tests if they fail
            # and rely on API connectivity test instead
            dns_ok = False
            socket_ok = False
            
            # Parse the endpoint to get hostname and port
            from urllib.parse import urlparse
            parsed_url = urlparse(self.search_endpoint)
            hostname = parsed_url.hostname
            port = parsed_url.port or 443
            
            # Test DNS resolution (optional for private endpoints)
            try:
                socket.gethostbyname(hostname)
                logger.info(f"DNS resolution successful for {hostname}")
                dns_ok = True
            except socket.gaierror as e:
                logger.warning(f"DNS resolution failed for {hostname}: {str(e)}")
                logger.info("This is expected for private endpoints accessed from outside the VNet")
            
            # Test connection to the endpoint (optional for private endpoints)
            if dns_ok:
                try:
                    sock = socket.create_connection((hostname, port), timeout=10)
                    sock.close()
                    logger.info(f"Connection successful to {hostname}:{port}")
                    socket_ok = True
                except Exception as e:
                    logger.warning(f"Socket connection failed to {hostname}:{port}: {str(e)}")
            
            # Test API availability - this is the most important test
            try:
                # Try to list indexes (this will work even if we don't have permission)
                list(self.index_client.list_indexes())
                logger.info("Azure AI Search API is accessible")
                return True
            except Exception as e:
                # Check if it's an authentication/authorization error vs connectivity
                if "401" in str(e) or "403" in str(e):
                    logger.info("API is accessible but authentication/authorization failed")
                    logger.info("This might be expected - continuing with the import process")
                    return True
                else:
                    logger.error(f"API connectivity test failed: {str(e)}")
                    return False
                
        except Exception as e:
            logger.error(f"Connectivity test failed: {str(e)}")
            return False
    
    def create_blob_indexer(self, 
                           storage_account_name: str,
                           container_name: str,
                           blob_name: str,
                           index_name: str,
                           storage_connection_string: Optional[str] = None) -> bool:
        """
        Create an indexer that directly reads CSV from blob storage into AI Search
        
        Args:
            storage_account_name: Azure storage account name
            container_name: Container name  
            blob_name: Blob (file) name
            index_name: Name for the search index
            storage_connection_string: Optional connection string (if None, uses managed identity)
            
        Returns:
            True if successful, False otherwise
        """
        try:
            logger.info("Creating blob storage indexer for direct data import...")
            
            # Generate unique names for data source and indexer
            data_source_name = f"{index_name}-datasource"
            indexer_name = f"{index_name}-indexer"
            
            # Step 1: Create or update data source connection
            if storage_connection_string:
                # Use connection string authentication
                connection_string = storage_connection_string
            else:
                # Use managed identity (recommended for private endpoints)
                connection_string = f"ResourceId=/subscriptions/{self._get_subscription_id()}/resourceGroups/{self._get_storage_resource_group(storage_account_name)}/providers/Microsoft.Storage/storageAccounts/{storage_account_name};"
            
            data_source = SearchIndexerDataSourceConnection(
                name=data_source_name,
                type="azureblob",
                connection_string=connection_string,
                container=SearchIndexerDataContainer(
                    name=container_name,
                    query=blob_name  # Specific file to index
                )
            )
            
            # Create or update the data source
            logger.info(f"Creating data source: {data_source_name}")
            self.indexer_client.create_or_update_data_source_connection(data_source)
            
            # Step 2: Create basic index schema for CSV (you may want to customize this)
            self._create_csv_index_schema(index_name)
            
            # Step 3: Create indexer with CSV parsing configuration
            indexer = SearchIndexer(
                name=indexer_name,
                data_source_name=data_source_name,
                target_index_name=index_name,
                parameters={
                    "configuration": {
                        "dataToExtract": "contentAndMetadata",
                        "parsingMode": "delimitedText",
                        "firstLineContainsHeaders": True,
                        "delimitedTextDelimiter": ",",
                        "delimitedTextHeaders": "auto"
                    }
                },
                field_mappings=[
                    FieldMapping(source_field_name="content", target_field_name="content"),
                    FieldMapping(source_field_name="metadata_storage_name", target_field_name="fileName"),
                    FieldMapping(source_field_name="metadata_storage_last_modified", target_field_name="lastModified")
                ]
            )
            
            # Create or update the indexer
            logger.info(f"Creating indexer: {indexer_name}")
            self.indexer_client.create_or_update_indexer(indexer)
            
            # Step 4: Run the indexer immediately
            logger.info("Starting indexer execution...")
            self.indexer_client.run_indexer(indexer_name)
            
            logger.info("Indexer created and started successfully!")
            logger.info(f"Data source: {data_source_name}")
            logger.info(f"Indexer: {indexer_name}")
            logger.info("The indexer will process the CSV file directly from blob storage.")
            
            return True
            
        except Exception as e:
            logger.error(f"Error creating blob indexer: {str(e)}")
            return False
    
    def _get_subscription_id(self) -> str:
        """Get current subscription ID"""
        try:
            import subprocess
            result = subprocess.run(['az', 'account', 'show', '--query', 'id', '-o', 'tsv'], 
                                  capture_output=True, text=True)
            return result.stdout.strip()
        except:
            return "YOUR_SUBSCRIPTION_ID"  # Fallback
    
    def _get_storage_resource_group(self, storage_account_name: str) -> str:
        """Get resource group for storage account"""
        try:
            import subprocess
            result = subprocess.run([
                'az', 'storage', 'account', 'show', 
                '--name', storage_account_name,
                '--query', 'resourceGroup', '-o', 'tsv'
            ], capture_output=True, text=True)
            return result.stdout.strip()
        except:
            return "YOUR_RESOURCE_GROUP"  # Fallback
    
    def _create_csv_index_schema(self, index_name: str) -> bool:
        """Create a flexible index schema for CSV data"""
        try:
            fields = [
                SimpleField(name="id", type=SearchFieldDataType.String, key=True),
                SearchableField(name="content", type=SearchFieldDataType.String, analyzer_name="standard.lucene"),
                SimpleField(name="fileName", type=SearchFieldDataType.String, filterable=True),
                SimpleField(name="lastModified", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
                # Add more fields as needed based on your CSV structure
                SearchableField(name="Column1", type=SearchFieldDataType.String, filterable=True),
                SearchableField(name="Column2", type=SearchFieldDataType.String, filterable=True),
                SearchableField(name="Column3", type=SearchFieldDataType.String, filterable=True),
                SearchableField(name="Column4", type=SearchFieldDataType.String, filterable=True),
                SearchableField(name="Column5", type=SearchFieldDataType.String, filterable=True)
            ]
            
            index = SearchIndex(name=index_name, fields=fields)
            
            try:
                self.index_client.create_index(index)
                logger.info(f"Created index: {index_name}")
            except ResourceExistsError:
                logger.info(f"Index {index_name} already exists")
            
            return True
            
        except Exception as e:
            logger.error(f"Error creating index schema: {str(e)}")
            return False
    
    def get_indexer_status(self, indexer_name: str) -> Dict[str, Any]:
        """Get the status of an indexer"""
        try:
            status = self.indexer_client.get_indexer_status(indexer_name)
            return {
                "status": status.status,
                "last_result": status.last_result.status if status.last_result else "No runs yet",
                "execution_history": [
                    {
                        "status": run.status,
                        "start_time": run.start_time,
                        "end_time": run.end_time,
                        "items_processed": run.item_count,
                        "items_failed": run.failed_item_count
                    }
                    for run in (status.execution_history or [])[:5]  # Last 5 runs
                ]
            }
        except Exception as e:
            logger.error(f"Error getting indexer status: {str(e)}")
            return {"error": str(e)}
    
    def import_csv_to_search(self, 
                           storage_account_name: str,
                           container_name: str,
                           blob_name: str,
                           index_name: str,
                           batch_size: int = 1000) -> bool:
        """
        Complete workflow to import CSV from blob storage to Azure AI Search
        
        Args:
            storage_account_name: Azure storage account name
            container_name: Container name
            blob_name: Blob (file) name
            index_name: Name for the search index
            batch_size: Batch size for document upload
            
        Returns:
            True if successful, False otherwise
        """
        try:
            logger.info("Starting CSV import to Azure AI Search...")
            
            # Step 1: Download CSV data from blob storage
            csv_data = self.get_blob_data(storage_account_name, container_name, blob_name)
            
            # Step 2: Parse CSV data
            documents = self.parse_csv_data(csv_data)
            
            if not documents:
                logger.error("No documents found in CSV data")
                return False
            
            # Step 3: Create search index schema
            index = self.infer_schema_from_data(documents, index_name)
            
            # Step 4: Create or update the index
            if not self.create_or_update_index(index):
                return False
            
            # Step 5: Upload documents
            if not self.upload_documents(index_name, documents, batch_size):
                return False
            
            logger.info("CSV import completed successfully!")
            return True
            
        except Exception as e:
            logger.error(f"Error during CSV import: {str(e)}")
            return False


def main():
    """Main function to run the CSV import"""
    
    # Configuration - Update these values for your environment
    SEARCH_SERVICE_NAME = "mcapsda-bicenterofexcellence-aisearch"  # Replace with your search service name
    SEARCH_ADMIN_KEY = None  # Optional: Replace with your admin key or leave None to use managed identity
    
    # Private Endpoint Configuration (uncomment and update if using private endpoint)
    # For private endpoints, use the standard FQDN which will resolve to private IP via private DNS zone
    CUSTOM_ENDPOINT = "https://mcapsda-bicenterofexcellence-aisearch.search.windows.net"
    
    STORAGE_ACCOUNT_NAME = "bicenterofexcellenceadls"  # Replace with your storage account name
    CONTAINER_NAME = "mcp"  # Replace with your container name
    BLOB_NAME = "OSOT Shortcuts.csv"  # Replace with your CSV file name
    INDEX_NAME = "shortcuts-search-index"  # Replace with desired index name
    
    # Choose import method
    USE_INDEXER = True  # Set to True for indexer-based import (recommended for private endpoints)
                       # Set to False for traditional download/upload approach
    
    try:
        # Check Azure CLI authentication
        logger.info("Checking Azure CLI authentication...")
        credential = AzureCliCredential()
        
        # Create importer instance
        importer = AzureSearchCSVImporter(
            search_service_name=SEARCH_SERVICE_NAME,
            search_admin_key=SEARCH_ADMIN_KEY,
            custom_endpoint=CUSTOM_ENDPOINT
        )
        
        if USE_INDEXER:
            logger.info("Using INDEXER-based import (recommended for private endpoints)")
            logger.info("This approach reads CSV directly from blob storage into AI Search")
            
            # Create blob indexer for direct import
            success = importer.create_blob_indexer(
                storage_account_name=STORAGE_ACCOUNT_NAME,
                container_name=CONTAINER_NAME,
                blob_name=BLOB_NAME,
                index_name=INDEX_NAME
            )
            
            if success:
                print(f"\n✅ Successfully created indexer for CSV import!")
                print(f"Index name: {INDEX_NAME}")
                print(f"Indexer name: {INDEX_NAME}-indexer")
                print(f"Data source: {INDEX_NAME}-datasource")
                endpoint_url = CUSTOM_ENDPOINT if CUSTOM_ENDPOINT else f"https://{SEARCH_SERVICE_NAME}.search.windows.net"
                print(f"Search endpoint: {endpoint_url}")
                print("\n📊 The indexer is now processing your CSV file...")
                print("You can monitor progress in the Azure Portal or check indexer status.")
                
                # Optionally check indexer status
                import time
                print("\n⏳ Waiting 10 seconds before checking initial status...")
                time.sleep(10)
                
                status = importer.get_indexer_status(f"{INDEX_NAME}-indexer")
                print(f"\n📈 Indexer Status: {status.get('status', 'Unknown')}")
                if status.get('last_result'):
                    print(f"Last run result: {status['last_result']}")
            else:
                print(f"\n❌ Failed to create indexer. Check the logs for details.")
                
        else:
            logger.info("Using TRADITIONAL download/upload approach")
            
            # Test connectivity (especially important for private endpoints)
            logger.info("Testing connectivity to Azure AI Search...")
            if not importer.test_connectivity():
                logger.warning("Connectivity test failed, but attempting to continue...")
                logger.info("For private endpoints, this may be expected if running from outside the VNet")
                print("\n⚠️ Connectivity test failed, but continuing with import attempt...")
                print("If you're using private endpoints and running from outside the Azure VNet,")
                print("consider running this script from:")
                print("1. An Azure VM within the same VNet")
                print("2. A machine connected via VPN or ExpressRoute")
                print("3. Azure Cloud Shell")
                # Don't return early - continue with the import attempt
            
            # Run the traditional import
            success = importer.import_csv_to_search(
                storage_account_name=STORAGE_ACCOUNT_NAME,
                container_name=CONTAINER_NAME,
                blob_name=BLOB_NAME,
                index_name=INDEX_NAME
            )
            
            if success:
                print(f"\n✅ Successfully imported CSV data to Azure AI Search!")
                print(f"Index name: {INDEX_NAME}")
                endpoint_url = CUSTOM_ENDPOINT if CUSTOM_ENDPOINT else f"https://{SEARCH_SERVICE_NAME}.search.windows.net"
                print(f"Search endpoint: {endpoint_url}")
            else:
                print(f"\n❌ Failed to import CSV data. Check the logs for details.")
            
    except Exception as e:
        logger.error(f"Script execution failed: {str(e)}")
        print(f"\n❌ Error: {str(e)}")
        print("\nTroubleshooting:")
        print("1. Make sure you're logged in to Azure CLI: az login")
        print("2. Verify your Azure subscription: az account show")
        print("3. Check that you have access to the storage account and search service")
        print("4. Ensure all required Python packages are installed")
        if CUSTOM_ENDPOINT:
            print("5. For private endpoints:")
            print("   - INDEXER approach (recommended): Works within Azure network")
            print("   - Traditional approach: Requires VNet connectivity")
            print("   - Consider running from Azure Cloud Shell, Azure VM, or via VPN")


if __name__ == "__main__":
    main()
