from sqlalchemy import Column, Integer, String, Text, DateTime, Float, Boolean, Enum as SAEnum, ForeignKey
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import relationship
from datetime import datetime
from backend.database import Base
import enum


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    USER = "user"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    phone = Column(String(20), nullable=True)
    avatar_url = Column(String(500), nullable=True)
    role = Column(SAEnum(UserRole), default=UserRole.USER, nullable=False, index=True)
    is_active = Column(Boolean, default=True)
    tokens_used = Column(Integer, default=0)
    last_login_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    projects = relationship("Project", back_populates="user", cascade="all, delete-orphan")
    token_records = relationship("TokenUsage", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User(id={self.id}, username={self.username}, role={self.role})>"


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    project_name = Column(String(200), nullable=False)
    project_type = Column(String(50), default="部门评价")
    status = Column(String(20), default="进行中")
    current_step = Column(Integer, default=0)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="projects")
    reports = relationship("Report", back_populates="project", cascade="all, delete-orphan")
    files = relationship("ProjectFile", back_populates="project", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Project(id={self.id}, name={self.project_name}, status={self.status})>"


class Report(Base):
    __tablename__ = "reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    step_number = Column(Integer, nullable=False)
    step_name = Column(String(100), nullable=False)
    content = Column(MEDIUMTEXT, nullable=True)
    model_used = Column(String(100), nullable=True)
    version = Column(Integer, default=1)
    is_final = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = relationship("Project", back_populates="reports")

    def __repr__(self):
        return f"<Report(id={self.id}, step={self.step_name}, version={self.version})>"


class ProjectFile(Base):
    __tablename__ = "project_files"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    original_name = Column(String(255), nullable=False)
    file_type = Column(String(20), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_size = Column(Integer, default=0)
    upload_type = Column(String(20), default="通用")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    expires_at = Column(DateTime, nullable=True)

    project = relationship("Project", back_populates="files")

    def __repr__(self):
        return f"<ProjectFile(id={self.id}, name={self.original_name})>"


class TokenUsage(Base):
    __tablename__ = "token_usage"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    session_id = Column(String(100), nullable=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)
    step_number = Column(Integer, nullable=True)
    model_name = Column(String(100), nullable=False)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    cost = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    user = relationship("User", back_populates="token_records")

    def __repr__(self):
        return f"<TokenUsage(id={self.id}, tokens={self.total_tokens})>"


class ModelConfig(Base):
    __tablename__ = "model_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(50), nullable=False)
    model_name = Column(String(100), nullable=False, index=True)
    model_id = Column(String(100), nullable=True)
    api_key = Column(String(500), nullable=True)
    api_base_url = Column(String(500), nullable=True)
    is_enabled = Column(Boolean, default=True)
    supports_vision = Column(Boolean, default=False)
    supports_file = Column(Boolean, default=False)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<ModelConfig(id={self.id}, name={self.model_name}, platform={self.platform})>"


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    step_number = Column(Integer, nullable=False, index=True)
    step_name = Column(String(100), nullable=False)
    prompt_content = Column(Text, nullable=True)
    is_system = Column(Boolean, default=False)
    # ── 粒度化字段（支持按指标层级/维度配置） ──
    indicator_level = Column(Integer, nullable=True)      # 1/2/3 级指标
    parent_indicator = Column(String(200), nullable=True)  # 所属上级指标名称
    dimension = Column(String(50), nullable=True)          # 维度: 决策/过程/产出/效益
    # ── 版本追踪 ──
    version = Column(Integer, default=1)
    modified_by = Column(Integer, nullable=True)
    change_log = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        extra = ""
        if self.indicator_level:
            extra += f", level={self.indicator_level}"
        if self.dimension:
            extra += f", dim={self.dimension}"
        return f"<PromptTemplate(id={self.id}, step={self.step_name}, version={self.version}{extra})>"


class KnowledgeBase(Base):
    __tablename__ = "knowledge_base"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    category = Column(String(100), nullable=True, index=True)
    step_number = Column(Integer, nullable=True, index=True)
    indicator_level = Column(Integer, nullable=True)
    parent_indicator = Column(String(200), nullable=True)  # 关联到具体上级指标
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<KnowledgeBase(id={self.id}, name={self.name})>"

class QuestionnaireAnswer(Base):
    """
    调查问卷答案存储表
    结构化存储李克特量表选择和开放题文本
    """
    __tablename__ = "questionnaire_answers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(100), nullable=False, index=True)
    question_id = Column(String(100), nullable=False)
    question_text = Column(Text, nullable=True)
    question_type = Column(String(20), nullable=False, default="likert")
    related_indicator_id = Column(String(100), nullable=True)
    related_indicator_name = Column(String(200), nullable=True)
    # 答案：李克特用 selected_level_index (0-4)；开放题用文本
    selected_level_index = Column(Integer, nullable=True)
    text_answer = Column(Text, nullable=True)
    submitter_ip = Column(String(50), nullable=True)
    submitted_at = Column(DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f"<QuestionnaireAnswer(id={self.id}, question={self.question_id}, type={self.question_type})>"


class WorkflowSession(Base):
    """
    工作流会话持久化表
    支持断点续跑、跨重启恢复
    """
    __tablename__ = "workflow_sessions"

