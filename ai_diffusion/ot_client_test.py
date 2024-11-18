import asyncio
import websockets
import json
from uuid import uuid4
from ai_diffusion.ot_client import (
    Operation, OperationType, PixelData, WebSocketMessage, MessageType
)

async def test_client(client_id: str):
    uri = "ws://localhost:8000/ws/test_client"

    async with websockets.connect(uri) as websocket:
        # Create a sample operation to send
        operation = Operation(
            operation_id=str(uuid4()),
            client_id=client_id,
            operation_type=OperationType.PIXEL_UPDATE,
            position={"x": 10, "y": 15},
            sequence_num=1,
            timestamp=1,
            base_version=1,
            data=PixelData(color=[255, 0, 0, 255], bounds={"x": 10, "y": 15, "width": 10, "height": 10}, layer_id="layer1"),
        )

        # Create a WebSocketMessage
        message = WebSocketMessage(
            type=MessageType.UPDATE,
            operation=operation,
            version=1,
            timestamp=1
        )

        # Send the message to the server
        await websocket.send(message.model_dump_json())
        print(f"Sent: {message}")

        # Wait for a response from the server
        response = await websocket.recv()
        response_data = json.loads(response)  # Parse the JSON string into a dictionary
        response_message = WebSocketMessage.model_validate_json(response_data)
        print(f"Received: {response_message}")

# Run the test client
if __name__ == "__main__":
    import argparse 
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-id", type=str, default=str(uuid4()))
    args = parser.parse_args()
    asyncio.get_event_loop().run_until_complete(test_client(args.client_id))
