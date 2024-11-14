# Real-time Collaboration Feature

## Overview
This feature enables real-time collaboration between multiple Krita users, allowing them to work on the same document simultaneously. Users can see each other's changes in real-time, including drawing operations and layer management.

## Key Features
- Real-time synchronization of drawing operations
- Layer management (create, delete, rename, move)
- Cursor position tracking
- Conflict resolution using Operational Transformation (OT)

## Technical Implementation

### WebSocket Communication
The plugin establishes a persistent WebSocket connection with a remote server, enabling:
- Bi-directional real-time communication
- Efficient message broadcasting between clients
- Automatic reconnection handling

### Operational Transformation (OT)
To maintain consistency across all clients, the plugin implements OT to handle concurrent operations:
- Each operation is tagged with Lamport timestamps for proper ordering
- Local operations are transformed against concurrent remote operations
- Server maintains the source of truth for document versions
- Conflicts are automatically resolved while preserving user intent

### Simple Example
When two users draw on the same area simultaneously:
1. User A draws a red pixel at position (100,100)
2. User B draws a blue pixel at position (100,100)
3. The OT system ensures both operations are preserved by transforming their positions
4. Both users see the same final result, maintaining document consistency

For developers interested in the implementation details, check the `ot_client.py` file in the `ai_diffusion` directory.
