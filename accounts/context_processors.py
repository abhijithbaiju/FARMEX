from .models import Notification

def notification_count(request):
    if request.user.is_authenticated:
        unread_count = Notification.objects.filter(user=request.user, is_read=False).count()
        notifications = Notification.objects.filter(user=request.user)[:10]
        return {
            'unread_notification_count': unread_count,
            'recent_notifications': notifications
        }
    return {
        'unread_notification_count': 0,
        'recent_notifications': []
    }
