from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('start/', views.quiz_start, name='quiz_start'),
    path('question/', views.quiz_question, name='quiz_question'),
    path('check/', views.check_answer, name='check_answer'),
    path('preflight/', views.api_preflight, name='api_preflight'),
    path('next/', views.next_question, name='next_question'),
    path('results/', views.quiz_results, name='quiz_results'),
    path('restart/', views.restart, name='restart'),
]
