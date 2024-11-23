from __future__ import annotations
import asyncio
from .websockets.src.websockets import client as websockets_client
from .websockets.src.websockets import exceptions as websockets_exceptions
from typing import Any, AsyncGenerator, Dict, List, Literal, Union, Optional
from dataclasses import dataclass, field
from uuid import uuid4
from enum import Enum
import logging as logger

from .client import Client, ClientMessage, ClientEvent, ClientFeatures, DeviceInfo
from .api import WorkflowInput
from .settings import PerformanceSettings

class OperationType(str, Enum):
    PIXEL_UPDATE = "pixel_update"
    LAYER_CREATE = "layer_create"
    LAYER_DELETE = "layer_delete"
    LAYER_MOVE = "layer_move"
    LAYER_RENAME = "layer_rename"

class MessageType(str, Enum):
    UPDATE = "update"
    ACK = "ack"
    ERROR = "error"

@dataclass
class PixelData:
    """Represents pixel-level changes in a document"""
    color: List[int] = field(default_factory=list)
    bounds: Dict[str, int] = field(default_factory=dict)
    layer_id: str = ""

@dataclass
class LayerData:
    """Represents layer-level changes in a document"""
    layer_id: str
    layer_name: str
    layer_type: str
    parent_id: Optional[str] = None
    above_id: Optional[str] = None

class Operation:
    """Represents a document operation with Lamport timestamp tracking"""
    client_id: str
    sequence_num: int
    timestamp: int
    operation_type: OperationType
    position: Dict[str, int]
    data: Union[PixelData, LayerData]
    base_version: int
    version: Optional[int] = None

    @property
    def operation_id(self) -> str:
        """Unique identifier combining client ID, sequence number, and timestamp"""
        return f"{self.client_id}:{self.sequence_num}:{self.timestamp}"

@dataclass
class WebSocketMessage:
    """Represents a WebSocket message for OT communication"""
    type: MessageType
    operation_id: Optional[str] = None
    operation: Optional[Operation] = None
    client_version: Optional[int] = None
    timestamp: Optional[int] = None
    error: Optional[str] = None
    version: Optional[int] = None

class OTClient(Client):
    """WebSocket client for handling Krita document synchronization with OT support"""

    @staticmethod
    async def connect(url: str, access_token: str = "") -> OTClient:
        """
        Creates and connects a new WebSocket client
        Used when: Initializing connection to server
        """
        client = OTClient(url)
        await client._connect()
        return client

    def __init__(self, url: str, client_id: str = ""):
        self.url = url
        self.device_info = DeviceInfo("local", "WebSocket Client", 0)
        self._ws = None
        self._current_operation: Optional[tuple[str, Operation]] = None
        self._queue: asyncio.Queue[tuple[str, Operation]] = asyncio.Queue()
        self.models = None  # Not implementing model management
 
        # OT-specific state
        self.client_id: str = f"{client_id}:{uuid4()}"  # Unique client identifier
        self.sequence_num: int = 0          # Local operation counter
        self.lamport_timestamp: int = 0     # Logical timestamp
        self._local_version: int = 0        # Local document version
        self._server_version: int = 0       # Last known server version
        self._pending_operations: list[Operation] = []  # Operations waiting for acknowledgment

    async def _connect(self) -> bool:
        """
        Establishes WebSocket connection
        Used when: Initial connection and reconnection attempts
        """
        try:
            self._ws = await websockets_client.connect(self.url)
            return True
        except Exception as e:
            print(f"Connection failed: {str(e)}")
            return False

    async def enqueue(self, work: WorkflowInput, front: bool = False) -> str:
        """
        Queues an update to be sent to the server with Lamport timestamp tracking
        Used when: Local document changes need to be synchronized
        
        Args:
            work: The operation to be applied (should contain operation_type, position, and data)
            front: Whether to prioritize this operation
        
        Returns:
            operation_id: Unique identifier for tracking this operation
        """
        # Increment local sequence number and timestamp
        self.sequence_num += 1
        self.lamport_timestamp += 1
 
        # Create operation with Lamport timestamp
        operation = Operation(
            client_id=self.client_id,
            sequence_num=self.sequence_num,
            timestamp=self.lamport_timestamp,
            operation_type=work.get("operation_type"),
            position=work.get("position", {}),
            data=work.get("data"),
            base_version=self._server_version
        )
 
        # Store operation in pending list
        self._pending_operations.append(operation)
        
        # Queue the operation
        if front:
            # Create a new queue with this operation at the front
            new_queue = asyncio.Queue()
            await new_queue.put((operation.operation_id, operation))
            while not self._queue.empty():
                item = await self._queue.get()
                await new_queue.put(item)
            self._queue = new_queue
        else:
            await self._queue.put((operation.operation_id, operation))
 
        return operation.operation_id

    async def _transform_operation(self, operation: Operation, concurrent_op: Operation) -> Operation:
        """
        Transforms an operation against a concurrent operation
        Used when: Need to adjust operation based on concurrent changes
        """
        # Update Lamport timestamp based on concurrent operation
        self.lamport_timestamp = max(self.lamport_timestamp, concurrent_op.timestamp) + 1
        
        # Basic position transformation example
        if operation.operation_type == "pixel_update" and concurrent_op.operation_type == "pixel_update":
            # If concurrent operation was at same position or before, adjust position
            if concurrent_op.position["x"] <= operation.position["x"]:
                operation.position["x"] += 1
            if concurrent_op.position["y"] <= operation.position["y"]:
                operation.position["y"] += 1
        
        return operation

    async def _handle_server_update(self, server_op: Operation):
        """
        Handles updates from server, transforming pending operations as needed
        Used when: Receiving server updates that might conflict with pending operations
        """
        server_version = server_op.version  # Use the resulting server version
        server_timestamp = server_op.timestamp
        
        # Update Lamport timestamp
        self.lamport_timestamp = max(self.lamport_timestamp, server_timestamp) + 1
        
        # Update server version
        if server_version > self._server_version:
            self._server_version = server_version
            
        # Transform any pending operations
        transformed_pending = []
        for pending_op in self._pending_operations:
            if pending_op.base_version < server_version:
                # Transform operation against server operation
                pending_op = await self._transform_operation(pending_op, server_op)
                pending_op.base_version = server_version
            transformed_pending.append(pending_op)
        
        self._pending_operations = transformed_pending

    async def listen(self) -> AsyncGenerator[ClientMessage, Any]:
        """
        Listen for messages and handle OT synchronization
        Used when: Continuous connection monitoring and update receiving
        """
        yield ClientMessage(ClientEvent.connected)

        while True:
            try:
                if self._ws is None or self._ws.closed:
                    yield ClientMessage(ClientEvent.disconnected)
                    break

                # Process queued updates
                if not self._current_operation and not self._queue.empty():
                    operation_id, operation = await self._queue.get()
                    self._current_operation = (operation_id, operation)
                    
                    # Send update to server with version information
                    message = WebSocketMessage(
                        type=MessageType.UPDATE,
                        operation_id=operation_id,
                        operation=operation,
                        client_version=self._local_version,
                        timestamp=self.lamport_timestamp
                    )
                    logger.debug(f"Sending message: {message}")
                    await self._ws.send(message.model_dump_json())

                # Receive server messages
                message = await self._ws.recv()
                data = WebSocketMessage.model_validate_json(message)
                logger.debug(f"Received message: {data}")
                message_type = data.type

                if message_type == MessageType.UPDATE:
                    await self._handle_server_update(data.operation)
                    
                    yield ClientMessage(
                        ClientEvent.output,
                        job_id=data.operation_id,
                        result=data.operation
                    )
                    
                elif message_type == MessageType.ACK:
                    op_id = data.operation_id
                    self._pending_operations = [op for op in self._pending_operations 
                                             if op.operation_id != op_id]
                    self._local_version += 1
                    
                    server_timestamp = data.timestamp
                    self.lamport_timestamp = max(self.lamport_timestamp, server_timestamp) + 1
                
                elif message_type == MessageType.ERROR:
                    yield ClientMessage(
                        ClientEvent.error,
                        job_id=data.operation_id,
                        error=data.error
                    )
                else:
                    # Handle unknown message type
                    logger.error(f"Unknown message type: {message_type}, message: {message}")
                    yield ClientMessage(
                        ClientEvent.error,
                        error=f"Unknown message type: {message_type}, message: {message}"
                    )

            except websockets_exceptions.ConnectionClosed:
                yield ClientMessage(ClientEvent.disconnected)
                break
            except Exception as e:
                yield ClientMessage(ClientEvent.error, error=str(e))

    async def interrupt(self):
        """
        Interrupts current operation
        Used when: Canceling current operation
        """
        self._current_operation = None

    async def disconnect(self):
        """
        Closes WebSocket connection
        Used when: Shutting down or cleaning up connection
        """
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def clear_queue(self):
        """
        Clears pending updates
        Used when: Resetting or clearing pending operations
        """
        self._queue: asyncio.Queue[tuple[str, Operation]] = asyncio.Queue()
        self._pending_operations: list[Operation] = []

    @property
    def features(self) -> ClientFeatures:
        """Returns supported features"""
        return ClientFeatures(
            ip_adapter=False,
            translation=False,
            languages=[],
            max_upload_size=0,
            max_control_layers=0
        )

    @property
    def performance_settings(self) -> PerformanceSettings:
        """Returns performance settings"""
        return PerformanceSettings()

# Example usage:
async def main():
    # Connect two clients to demonstrate collaboration
    client1 = await OTClient.connect("ws://localhost:8000")
    client2 = await OTClient.connect("ws://localhost:8000")
    
    # Listen for updates on client1
    async def listen_client1():
        async for message in client1.listen():
            if message.event == ClientEvent.output:
                # Server broadcasted an operation
                operation = message.result
                print(f"Client 1 received operation: {operation['operation_type']}")
                if operation['operation_type'] == 'pixel_update':
                    print(f"Pixel update at {operation['position']}")
                    print(f"Color: {operation['data']['color']}")
                elif operation['operation_type'] == 'layer_create':
                    print(f"New layer: {operation['data']['layer_name']}")
            elif message.event == ClientEvent.error:
                print(f"Client 1 error: {message.error}")

    # Listen for updates on client2
    async def listen_client2():
        async for message in client2.listen():
            if message.event == ClientEvent.output:
                operation = message.result
                print(f"Client 2 received operation: {operation['operation_type']}")
            elif message.event == ClientEvent.error:
                print(f"Client 2 error: {message.error}")

    # Simulate drawing operations
    async def simulate_drawing():
        # Client 1 creates a new layer
        await client1.enqueue({
            "operation_type": OperationType.LAYER_CREATE,
            "position": {"x": 0, "y": 0},
            "data": LayerData(
                layer_id="layer1",
                layer_name="Drawing Layer",
                layer_type="paintlayer"
            )
        })

        # Wait for layer creation to be processed
        await asyncio.sleep(1)

        # Client 1 draws a red pixel
        await client1.enqueue({
            "operation_type": OperationType.PIXEL_UPDATE,
            "position": {"x": 100, "y": 100},
            "data": PixelData(
                color=[255, 0, 0, 255],  # Red pixel
                bounds={"x": 100, "y": 100, "width": 1, "height": 1},
                layer_id="layer1"
            )
        })

        # Client 2 draws a blue pixel concurrently
        await client2.enqueue({
            "operation_type": OperationType.PIXEL_UPDATE,
            "position": {"x": 150, "y": 150},
            "data": PixelData(
                color=[0, 0, 255, 255],  # Blue pixel
                bounds={"x": 150, "y": 150, "width": 1, "height": 1},
                layer_id="layer1"
            )
        })

        # Client 1 renames the layer
        await client1.enqueue({
            "operation_type": OperationType.LAYER_RENAME,
            "position": {"x": 0, "y": 0},
            "data": LayerData(
                layer_id="layer1",
                layer_name="Collaborative Drawing",
                layer_type="paintlayer"
            )
        })

    # Run everything concurrently
    await asyncio.gather(
        listen_client1(),
        listen_client2(),
        simulate_drawing()
    )

    # Cleanup
    await client1.disconnect()
    await client2.disconnect()

if __name__ == "__main__":
    # Expected server messages:
    # 1. Client 1 creates layer -> Server broadcasts to Client 2
    # 2. Client 1 draws pixel -> Server transforms if needed, broadcasts to Client 2
    # 3. Client 2 draws pixel -> Server transforms if needed, broadcasts to Client 1
    # 4. Client 1 renames layer -> Server broadcasts to Client 2
    asyncio.run(main())