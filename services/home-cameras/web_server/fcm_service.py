"""
Firebase Cloud Messaging Service for push notifications
Integrates with the HomeCameras backend to send push notifications to Android clients
"""

import logging
import json
from typing import Dict, List, Optional, Any, TYPE_CHECKING
from dataclasses import dataclass, asdict
from datetime import datetime

if TYPE_CHECKING:
    from firebase_admin import messaging

logger = logging.getLogger(__name__)

# Try to import firebase_admin, but make it optional for development
try:
    import firebase_admin
    from firebase_admin import credentials, messaging
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False
    logger.warning("firebase-admin not installed. Push notifications will be disabled.")

@dataclass
class DeviceInfo:
    """Information about a registered device"""
    device_token: str
    device_name: str
    platform: str
    registered_at: float
    last_active: float = 0.0

@dataclass
class NotificationData:
    """Data for a push notification"""
    camera_ip: str
    camera_name: Optional[str] = None
    zone: Optional[str] = None
    timestamp: float = 0.0
    confidence: Optional[float] = None
    detected_objects: Optional[str] = None
    type: str = "motion"

class FCMService:
    """
    Firebase Cloud Messaging service for sending push notifications
    """
    
    def __init__(self, service_account_path: Optional[str] = None):
        """
        Initialize FCM service
        
        Args:
            service_account_path: Path to Firebase service account JSON file
        """
        self.devices: Dict[str, DeviceInfo] = {}
        self.initialized = False
        
        if not FIREBASE_AVAILABLE:
            logger.warning("Firebase Admin SDK not available. Install with: pip install firebase-admin")
            return
        
        if service_account_path:
            try:
                cred = credentials.Certificate(service_account_path)
                firebase_admin.initialize_app(cred)
                self.initialized = True
                logger.info("FCM service initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize FCM service: {e}")
        else:
            logger.info("FCM service created but not initialized (no service account path)")
    
    def register_device(self, device_token: str, device_name: str, platform: str) -> bool:
        """
        Register a new device for push notifications
        
        Args:
            device_token: FCM device token
            device_name: Human-readable device name
            platform: Platform type (android, ios, etc.)
            
        Returns:
            True if registration successful
        """
        if not self.initialized:
            logger.warning("FCM service not initialized, device not registered")
            return False
        
        try:
            self.devices[device_token] = DeviceInfo(
                device_token=device_token,
                device_name=device_name,
                platform=platform,
                registered_at=datetime.now().timestamp(),
                last_active=datetime.now().timestamp()
            )
            logger.info(f"Device registered: {device_name} ({platform})")
            return True
        except Exception as e:
            logger.error(f"Failed to register device: {e}")
            return False
    
    def unregister_device(self, device_token: str) -> bool:
        """
        Unregister a device from push notifications
        
        Args:
            device_token: FCM device token to remove
            
        Returns:
            True if unregistration successful
        """
        try:
            if device_token in self.devices:
                del self.devices[device_token]
                logger.info(f"Device unregistered: {device_token}")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to unregister device: {e}")
            return False
    
    def send_motion_notification(
        self,
        camera_ip: str,
        camera_name: Optional[str] = None,
        zone: Optional[str] = None,
        confidence: Optional[float] = None,
        detected_objects: Optional[List[Dict[str, Any]]] = None,
        device_tokens: Optional[List[str]] = None
    ) -> Any:
        """
        Send a motion detection notification to devices
        
        Args:
            camera_ip: IP address of the camera that detected motion
            camera_name: Optional human-readable camera name
            zone: Name of the motion zone
            confidence: Detection confidence score
            detected_objects: List of detected objects with class and confidence
            device_tokens: List of device tokens to send to (None for all devices)
            
        Returns:
            BatchResponse from Firebase
        """
        if not self.initialized:
            logger.warning("FCM service not initialized, notification not sent")
            return None
        
        # Build notification data
        notification_data = {
            "camera_ip": camera_ip,
            "camera_name": camera_name or camera_ip,
            "zone": zone or "Motion Zone",
            "timestamp": str(int(datetime.now().timestamp())),
            "type": "motion"
        }
        
        if confidence is not None:
            notification_data["confidence"] = str(round(confidence, 2))
        
        if detected_objects:
            objects_summary = ", ".join([
                f"{obj.get('class_name', 'unknown')}({obj.get('confidence', 0):.2f})"
                for obj in detected_objects
            ])
            notification_data["detected_objects"] = objects_summary
        
        # Build notification
        notification = messaging.Notification(
            title=f"Motion Detected - {camera_name or camera_ip}",
            body=self._build_notification_body(zone, detected_objects, confidence)
        )
        
        # Get target device tokens
        if device_tokens is None:
            device_tokens = list(self.devices.keys())
        
        if not device_tokens:
            logger.warning("No device tokens to send notification to")
            return None
        
        # Send notification to each device
        responses = []
        for token in device_tokens:
            try:
                message = messaging.Message(
                    notification=notification,
                    data=notification_data,
                    token=token,
                    android=messaging.AndroidConfig(
                        priority="high",
                        notification=messaging.AndroidNotification(
                            icon="ic_notification",
                            color="#F44336",
                            sound="default",
                            click_action="com.homecameras.android.ACTION_VIEW_CAMERA"
                        )
                    ),
                    apns=messaging.APNSConfig(
                        payload=messaging.APNSPayload(
                            aps=messaging.Aps(
                                sound="default",
                                category="MOTION_ALERT"
                            )
                        )
                    )
                )
                
                response = messaging.send(message)
                responses.append(response)
                logger.debug(f"Notification sent to device: {token}")
                
            except Exception as e:
                logger.error(f"Failed to send notification to {token}: {e}")
                # Remove invalid token
                if "not registered" in str(e).lower() or "not valid" in str(e).lower():
                    self.unregister_device(token)
        
        logger.info(f"Sent {len(responses)} motion notifications")
        return responses
    
    def send_camera_status_notification(
        self,
        camera_ip: str,
        camera_name: Optional[str] = None,
        is_online: bool = False,
        device_tokens: Optional[List[str]] = None
    ) -> Any:
        """
        Send a camera status notification (online/offline)
        
        Args:
            camera_ip: IP address of the camera
            camera_name: Optional human-readable camera name
            is_online: Whether the camera is online or offline
            device_tokens: List of device tokens to send to
            
        Returns:
            BatchResponse from Firebase
        """
        if not self.initialized:
            return None
        
        status_text = "online" if is_online else "offline"
        
        notification_data = {
            "camera_ip": camera_ip,
            "camera_name": camera_name or camera_ip,
            "type": "status",
            "status": status_text,
            "timestamp": str(int(datetime.now().timestamp()))
        }
        
        notification = messaging.Notification(
            title=f"Camera {status_text.title()} - {camera_name or camera_ip}",
            body=f"Camera {camera_name or camera_ip} is now {status_text}"
        )
        
        if device_tokens is None:
            device_tokens = list(self.devices.keys())
        
        for token in device_tokens:
            try:
                message = messaging.Message(
                    notification=notification,
                    data=notification_data,
                    token=token,
                    android=messaging.AndroidConfig(
                        priority="normal",
                        notification=messaging.AndroidNotification(
                            icon="ic_notification",
                            color="#FF9800" if not is_online else "#4CAF50"
                        )
                    )
                )
                messaging.send(message)
            except Exception as e:
                logger.error(f"Failed to send status notification: {e}")
        
        return None
    
    def _build_notification_body(
        self,
        zone: Optional[str],
        detected_objects: Optional[List[Dict[str, Any]]],
        confidence: Optional[float]
    ) -> str:
        """Build a human-readable notification body"""
        parts = []
        
        if zone:
            parts.append(f"Zone: {zone}")
        
        if detected_objects:
            objects = [obj.get("class_name", "object") for obj in detected_objects]
            parts.append(f"Detected: {', '.join(objects)}")
        
        if confidence is not None:
            parts.append(f"Confidence: {confidence:.0%}")
        
        return " | ".join(parts) if parts else "Motion detected"
    
    def get_registered_devices(self) -> List[Dict[str, Any]]:
        """Get list of registered devices"""
        return [asdict(device) for device in self.devices.values()]
    
    def cleanup_invalid_tokens(self) -> int:
        """
        Clean up invalid device tokens
        Returns number of tokens removed
        """
        # This would typically involve sending a test message and removing failures
        # For now, we'll just remove devices that haven't been active recently
        cutoff_time = datetime.now().timestamp() - (30 * 24 * 60 * 60)  # 30 days
        to_remove = [
            token for token, device in self.devices.items()
            if device.last_active < cutoff_time
        ]
        
        for token in to_remove:
            del self.devices[token]
        
        return len(to_remove)


# Global FCM service instance
_fcm_service: Optional[FCMService] = None

def get_fcm_service(service_account_path: Optional[str] = None) -> FCMService:
    """
    Get or create the global FCM service instance
    
    Args:
        service_account_path: Path to Firebase service account JSON file
        
    Returns:
        FCMService instance
    """
    global _fcm_service
    
    if _fcm_service is None:
        _fcm_service = FCMService(service_account_path)
    
    return _fcm_service


def init_fcm_service(service_account_path: str) -> FCMService:
    """
    Initialize the global FCM service with a service account
    
    Args:
        service_account_path: Path to Firebase service account JSON file
        
    Returns:
        FCMService instance
    """
    global _fcm_service
    _fcm_service = FCMService(service_account_path)
    return _fcm_service