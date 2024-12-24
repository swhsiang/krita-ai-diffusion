from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from typing import Dict
import asyncio
import json
import logging as logger
from ot_plugin.ot_client import (
    OperationType, MessageType, PixelData, LayerData, Operation, WebSocketMessage
)
import uvicorn

app = FastAPI()

clients: Dict[str, WebSocket] = {}
pending_operations: Dict[str, list[Operation]] = {}
server_version = 0

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, client_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[client_id] = websocket

    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]

    async def send_personal_message(self, message: WebSocketMessage, websocket: WebSocket):
        await websocket.send_json(message.to_json())

    async def broadcast(self, message: WebSocketMessage):
        message_json = message.to_json()
        for client_id, connection in self.active_connections.items():
            if client_id == message.client_id:
                continue
            await connection.send_json(message_json)

manager = ConnectionManager()

async def transform_operation(operation: Operation, concurrent_op: Operation) -> Operation:
    """ 
    Transforms an operation against a concurrent operation, using the same logic
    as defined in the OTClient._transform_operation method.
    """
    lamport_timestamp = max(operation.timestamp, concurrent_op.timestamp) + 1

    if operation.operation_type == OperationType.PIXEL_UPDATE and concurrent_op.operation_type == OperationType.PIXEL_UPDATE:
        if concurrent_op.position["x"] <= operation.position["x"]:
            operation.position["x"] += 1
        if concurrent_op.position["y"] <= operation.position["y"]:
            operation.position["y"] += 1

    operation.timestamp = lamport_timestamp
    return operation

async def handle_operation(client_id: str, operation: Operation):
    global server_version

    # Transform any pending operations
    if client_id in pending_operations:
        transformed_pending = []
        for pending_op in pending_operations[client_id]:
            if pending_op.base_version < server_version:
                pending_op = await transform_operation(pending_op, operation)
                pending_op.base_version = server_version
            transformed_pending.append(pending_op)
        
        pending_operations[client_id] = transformed_pending
    else:
        pending_operations[client_id] = []

    # Update server version
    server_version += 1
    operation.version = server_version

    # Broadcast operation to all clients
    message = WebSocketMessage(
        type=MessageType.UPDATE, 
        operation=operation, 
        version=server_version,
        timestamp=operation.timestamp
    )
    logger.info(f"Broadcasting message: {message.to_json()}")
    # FIXME broadcast later, print the message first.
    # await manager.broadcast(message)

    # Acknowledge the client
    ack_message = WebSocketMessage(
        type=MessageType.ACK, 
        operation_id=operation.operation_id, 
        client_version=operation.sequence_num,
        timestamp=operation.timestamp
    )
    await manager.send_personal_message(ack_message, clients[client_id])

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await manager.connect(client_id, websocket)
    clients[client_id] = websocket
    try:
        while True:
            data = await websocket.receive_json()
            message = WebSocketMessage.model_validate(data)
            if message.type == MessageType.UPDATE and message.operation:
                await handle_operation(client_id, message.operation)
            else:
                logger.error(f"Unknown message type: {message.type}")
                error_message = WebSocketMessage(
                    type=MessageType.ERROR, 
                    error=f"Unknown message type: {message.type}"
                )
                await manager.send_personal_message(error_message, websocket)
    except WebSocketDisconnect:
        manager.disconnect(client_id)
        del clients[client_id]
        logger.info(f"Client {client_id} disconnected.")

if __name__ == "__main__":
    # Setup the logger
    logger.basicConfig(level=logger.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8080)
