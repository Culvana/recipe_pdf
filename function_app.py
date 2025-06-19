# function_app.py
import azure.functions as func
import azure.durable_functions as df
from azure.cosmos import CosmosClient
import logging
import os
from typing import List, Dict, Any
import json
import base64
import tempfile
import shutil
from main import PDFGenerator, WordGenerator, OpenAIHelper
from datetime import datetime

# Initialize logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Function App
app = df.DFApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# Initialize Cosmos DB client
cosmos_client = CosmosClient(
    os.environ["COSMOS_ENDPOINT"],
    os.environ["COSMOS_KEY"]
)

# Get database and container references
database = cosmos_client.get_database_client(os.environ["COSMOS_DATABASE"])
container = database.get_container_client(os.environ["COSMOS_CONTAINER"])

@app.route(route="generate_recipes/{user_id}", methods=["POST"])
@app.durable_client_input(client_name="client")
async def http_start(req: func.HttpRequest, client: df.DurableOrchestrationClient) -> func.HttpResponse:
    try:
        # Get user_id from route
        user_id = req.route_params.get('user_id')
        if not user_id:
            return func.HttpResponse(
                json.dumps({"error": "Please provide a user_id in the URL"}),
                status_code=400,
                mimetype="application/json"
            )

        # Parse request body
        try:
            req_body = req.get_json()
            recipe_ids = req_body.get('recipe_names', [])
            format_type = req_body.get('format', 'pdf').lower()
            download = req_body.get('download', False)
        except ValueError:
            return func.HttpResponse(
                json.dumps({"error": "Invalid request body"}),
                status_code=400,
                mimetype="application/json"
            )

        if not recipe_ids:
            return func.HttpResponse(
                json.dumps({"error": "Please provide recipe_ids in the request body"}),
                status_code=400,
                mimetype="application/json"
            )

        if format_type not in ['pdf', 'docx']:
            return func.HttpResponse(
                json.dumps({"error": "Format must be either 'pdf' or 'docx'"}),
                status_code=400,
                mimetype="application/json"
            )

        # Generate unique instance ID
        instance_id = f"recipes-{user_id}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"

        # Start orchestration
        await client.start_new(
            "RecipeOrchestrator",
            instance_id,
            {
                "user_id": user_id,
                "recipe_ids": recipe_ids,
                "format": format_type,
                "download": download
            }
        )
        
        # Check status
        status = await client.get_status(instance_id)
        if status and status.runtime_status == df.OrchestrationRuntimeStatus.Completed:
            result = status.output
            if result and result.get('success'):
                content = result['documents'].get(format_type)
                if download and content:
                    filename = f"recipes_{user_id}.{format_type}"
                    mime_type = {
                        'pdf': 'application/pdf',
                        'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                    }[format_type]
                    
                    return func.HttpResponse(
                        body=base64.b64decode(content),
                        headers={
                            'Content-Type': mime_type,
                            'Content-Disposition': f'attachment; filename="{filename}"',
                            'Access-Control-Expose-Headers': 'Content-Disposition'
                        }
                    )
                    
                return func.HttpResponse(
                    json.dumps(result),
                    mimetype="application/json"
                )
        
        return client.create_check_status_response(req, instance_id)
        
    except Exception as e:
        logger.error(f"Error in HTTP start: {str(e)}")
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            status_code=500,
            mimetype="application/json"
        )

@app.orchestration_trigger(context_name="context")
def RecipeOrchestrator(context: df.DurableOrchestrationContext):
    try:
        input_data = context.get_input()
        user_id = input_data["user_id"]
        recipe_ids = input_data["recipe_ids"]
        format_type = input_data["format"]
        
        # Get recipes
        recipes = yield context.call_activity("GetRecipes", {
            "user_id": user_id,
            "recipe_ids": recipe_ids
        })
        
        if not recipes:
            return {
                "success": False,
                "message": "No recipes found"
            }

        # Generate documents
        documents = yield context.call_activity("GenerateDocuments", {
            "user_id": user_id,
            "recipes_data": recipes,
            "format": format_type
        })
        
        return {
            "success": True,
            "documents": documents
        }

    except Exception as e:
        logger.error(f"Error in orchestrator: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }

@app.activity_trigger(input_name="inputdata")
def GetRecipes(inputdata: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        user_id = inputdata["user_id"]
        recipe_ids = inputdata["recipe_ids"]
        
        query = "SELECT * FROM c WHERE c.id = @id"
        params = [{"name": "@id", "value": user_id}]
        
        items = list(container.query_items(
            query=query,
            parameters=params,
            enable_cross_partition_query=True
        ))
        
        if items:
            user_doc = items[0]
            inventory_key = f"inventory-items-{user_id}"
            
            if inventory_key in user_doc.get('recipes', {}):
                recipes = user_doc['recipes'][inventory_key]
                if recipe_ids:
                    recipes = [r for r in recipes if r['name'] in recipe_ids]
                if recipes:
                    return recipes
                    
        return []
        
    except Exception as e:
        logger.error(f"Error getting recipes: {str(e)}")
        raise

@app.activity_trigger(input_name="inputdata")
def GenerateDocuments(inputdata: Dict[str, Any]) -> Dict[str, str]:
    try:
        recipes = inputdata["recipes_data"]
        user_id = inputdata["user_id"]
        format_type = inputdata.get("format", "pdf")
        
        if not recipes:
            raise ValueError("No recipes data provided")

        # Initialize helpers
        openai_helper = OpenAIHelper()
        pdf_generator = PDFGenerator()
        word_generator = WordGenerator()
        
        # Generate AI instructions
        ai_instructions_list = []
        for recipe in recipes:
            ai_instructions = openai_helper.generate_instructions(
                recipe['name'],
                recipe['data']['ingredients']
            )
            ai_instructions_list.append(ai_instructions)

        # Create temporary directory
        temp_dir = tempfile.mkdtemp()
        try:
            # Generate documents
            pdf_path = os.path.join(temp_dir, f"recipes_{user_id}.pdf")
            docx_path = os.path.join(temp_dir, f"recipes_{user_id}.docx")
            
            # Generate requested format
            if format_type == 'pdf':
                pdf_generator.create_recipe_pdf(recipes, ai_instructions_list, pdf_path)
                with open(pdf_path, 'rb') as f:
                    return {"pdf": base64.b64encode(f.read()).decode()}
            else:
                word_generator.create_recipe_docx(recipes, ai_instructions_list, docx_path)
                with open(docx_path, 'rb') as f:
                    return {"docx": base64.b64encode(f.read()).decode()}
            
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
        
    except Exception as e:
        logger.error(f"Error generating documents: {e}")
        raise
