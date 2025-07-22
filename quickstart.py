import os 
import sys

class Colors:
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BLUE = '\033[94m'
    END = '\033[0m'
    BOLD = '\033[1m'

def create_env_file():
    """Create .env file from template if it doesn't exist"""
    if not os.path.exists('.env') and os.path.exists('.env.example'):
        print(f"\n{Colors.YELLOW}Creating .env file from template...{Colors.END}")
        with open('.env.example', 'r') as src, open('.env', 'w') as dst:
            dst.write(src.read())
        print(f"{Colors.GREEN}✓ Created .env file. Please edit it with your credentials.{Colors.END}")
        return False
    return True

def check_python_version():
    """Check if Python version is 3.8+"""
    print(f"\n{Colors.BLUE}Checking Python version...{Colors.END}")
    version = sys.version_info
    if version.major >= 3 and version.minor >= 8:
        print(f"{Colors.GREEN}✓ Python {version.major}.{version.minor}.{version.micro} is supported{Colors.END}")
        return True
    else:
        print(f"{Colors.RED}✗ Python {version.major}.{version.minor} is not supported. Please use Python 3.8+{Colors.END}")
        return False

def check_adomd():
    """Check if ADOMD.NET is available"""
    print(f"\n{Colors.BLUE}Checking ADOMD.NET...{Colors.END}")
    
    try:
        import clr # type: ignore
        adomd_paths = [
            r"C:\Program Files\Microsoft.NET\ADOMD.NET\160",
            r"C:\Program Files\Microsoft.NET\ADOMD.NET\150",
            r"C:\Program Files (x86)\Microsoft.NET\ADOMD.NET\160",
            r"C:\Program Files (x86)\Microsoft.NET\ADOMD.NET\150"
        ]
        
        adomd_found = False
        for path in adomd_paths:
            if os.path.exists(path):
                try:
                    sys.path.append(path)
                    clr.AddReference("Microsoft.AnalysisServices.AdomdClient")
                    adomd_found = True
                    print(f"{Colors.GREEN}✓ ADOMD.NET found at: {path}{Colors.END}")
                    break
                except:
                    continue
        
        if not adomd_found:
            print(f"{Colors.RED}✗ ADOMD.NET not found{Colors.END}")
            print(f"{Colors.YELLOW}Please install SQL Server Management Studio (SSMS) or ADOMD.NET client{Colors.END}")
            return False
            
    except Exception as e:
        print(f"{Colors.RED}✗ Error checking ADOMD.NET: {str(e)}{Colors.END}")
        return False
    
    return True

def check_dependencies():
    """Check if all required packages are installed"""
    print(f"\n{Colors.BLUE}Checking dependencies...{Colors.END}")
    
    required_packages = {
        'mcp': 'mcp',
        'pyadomd': 'pyadomd',
        'openai': 'openai',
        'dotenv': 'python-dotenv',
        'clr': 'pythonnet'
    }
    
    missing_packages = []
    
    for import_name, package_name in required_packages.items():
        try:
            if import_name == 'dotenv':
                import dotenv # type: ignore
            elif import_name == 'clr':
                import clr # type: ignore
            elif import_name == 'mcp':
                import mcp # type: ignore
            elif import_name == 'pyadomd':
                import pyadomd # type: ignore
            elif import_name == 'openai':
                import openai # type: ignore
            else:
                __import__(import_name)
            print(f"{Colors.GREEN}✓ {package_name} is installed{Colors.END}")
        except ImportError:
            print(f"{Colors.RED}✗ {package_name} is not installed{Colors.END}")
            missing_packages.append(package_name)
    
    if missing_packages:
        print(f"\n{Colors.YELLOW}To install missing packages, run:{Colors.END}")
        print(f"pip install {' '.join(missing_packages)}")
        return False
    
    return True

def check_environment():
    """Check environment variables"""    
    return True

def cognitiveservices_authenticate():
    """Authenticate with Azure Cognitive Services using DefaultAzureCredential"""
    try:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider # type: ignore
        from azure.core.exceptions import ClientAuthenticationError # type: ignore
        cognitiveservices_token_provider = get_bearer_token_provider(
            DefaultAzureCredential(),
            "https://cognitiveservices.azure.com/.default"
        )
        return cognitiveservices_token_provider
    except ClientAuthenticationError as e:
        print("Authentication failed:", e)
        return None
    except Exception as e:
        print("Unexpected error:", e)
        return None

def semantic_model_authenticate():
    """Authenticate with Azure Semantic Model using DefaultAzureCredential"""
    try:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider # type: ignore
        from azure.core.exceptions import ClientAuthenticationError # type: ignore
        semantic_model_token_provider = get_bearer_token_provider(
            DefaultAzureCredential(),
            "https://semanticmodel.azure.com/.default"
        )
        return semantic_model_token_provider
    except ClientAuthenticationError as e:
        print("Authentication failed:", e)
        return None
    except Exception as e:
        print("Unexpected error:", e)
        return None

def main():
    """create .env file if it doesn't exist"""
    env_exists = create_env_file()
    
    # Run all checks
    checks = [
        ("Python Version", check_python_version),
        ("ADOMD.NET", check_adomd),
        ("Dependencies", check_dependencies),
        ("Environment", check_environment),
    ]
    
    all_passed = True
    results = {}
    
    for name, check_func in checks:
        results[name] = check_func()
        all_passed = all_passed and results[name]

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}Setup cancelled by user{Colors.END}")
    except Exception as e:
        print(f"\n{Colors.RED}Error: {str(e)}{Colors.END}")
        sys.exit(1)