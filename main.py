import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api_run_endpoint import router
from admin import router as admin_router

app = FastAPI(title='Puhti Run API')

_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    'CORS_ALLOWED_ORIGINS',
    'https://hbv.we3data.com,https://diwa-data-lab-vre.rahtiapp.fi'
).split(',') if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=['GET', 'POST', 'DELETE'],
    allow_headers=['Content-Type', 'x-jupyterhub-token'],
    expose_headers=[],
)

app.include_router(router)
app.include_router(admin_router)


@app.get('/health')
def health():
    return {'status': 'ok'}


