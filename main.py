import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI

import database
import core
import portal_routes
import admin_routes
import olt_routes
import settings_routes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("billing_app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    core.get_webhook_secret()
    logger.info(f"Database initialized (PostgreSQL={database.IS_POSTGRES}).")
    yield


app = FastAPI(title="ISP Billing, MikroTik & OLT Automation", lifespan=lifespan)

app.include_router(portal_routes.router)
app.include_router(admin_routes.router)
app.include_router(olt_routes.router)
app.include_router(settings_routes.router)
