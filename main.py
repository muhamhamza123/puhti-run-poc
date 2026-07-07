from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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


