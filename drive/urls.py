from django.urls import path

from . import views

urlpatterns = [
    path('auth/google', views.auth_google_start, name='auth_google_start'),
    path(
        'auth/google/callback',
        views.auth_google_callback,
        name='auth_google_callback',
    ),
    path('auth/me', views.auth_me, name='auth_me'),
    path('auth/logout', views.auth_logout, name='auth_logout'),
    path('folders', views.folders_list, name='folders_list'),
    path('documents', views.documents_list, name='documents_list'),
    path('chat', views.chat, name='chat'),
]
