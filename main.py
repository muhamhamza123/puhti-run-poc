import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from api_run_endpoint import router

app = FastAPI(title='Puhti Run API')

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)

app.include_router(router)


@app.get('/health')
def health():
    return {'status': 'ok'}


@app.get('/launcher_widget.py')
def serve_launcher():
    path = os.path.join(os.path.dirname(__file__), 'launcher_widget.py')
    return FileResponse(path, media_type='text/plain')
