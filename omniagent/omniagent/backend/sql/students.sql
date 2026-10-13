DROP TABLE IF EXISTS academic_records;
DROP TABLE IF EXISTS attendance;
DROP TABLE IF EXISTS enrollments;
DROP TABLE IF EXISTS courses;
DROP TABLE IF EXISTS students;

CREATE TABLE students (
  id INT AUTO_INCREMENT PRIMARY KEY,
  enrollment_no VARCHAR(20) NOT NULL UNIQUE,
  full_name VARCHAR(120) NOT NULL,
  email VARCHAR(160) NOT NULL,
  phone VARCHAR(20),
  branch VARCHAR(60) NOT NULL,
  semester TINYINT NOT NULL,
  enrollment_year SMALLINT NOT NULL,
  status ENUM('active','on_leave','graduated') NOT NULL DEFAULT 'active',
  cgpa DECIMAL(4,2),
  INDEX idx_students_branch (branch)
) ENGINE=InnoDB;

CREATE TABLE courses (
  id INT AUTO_INCREMENT PRIMARY KEY,
  code VARCHAR(12) NOT NULL UNIQUE,
  title VARCHAR(120) NOT NULL,
  branch VARCHAR(60) NOT NULL,
  credits TINYINT NOT NULL,
  instructor VARCHAR(100) NOT NULL,
  semester TINYINT NOT NULL
) ENGINE=InnoDB;

CREATE TABLE enrollments (
  id INT AUTO_INCREMENT PRIMARY KEY,
  student_id INT NOT NULL,
  course_id INT NOT NULL,
  UNIQUE KEY uq_enroll (student_id, course_id),
  FOREIGN KEY (student_id) REFERENCES students(id),
  FOREIGN KEY (course_id) REFERENCES courses(id)
) ENGINE=InnoDB;

CREATE TABLE attendance (
  id INT AUTO_INCREMENT PRIMARY KEY,
  student_id INT NOT NULL,
  course_id INT NOT NULL,
  attendance_date DATE NOT NULL,
  status ENUM('present','absent','late') NOT NULL,
  INDEX idx_att_course (course_id, attendance_date),
  FOREIGN KEY (student_id) REFERENCES students(id),
  FOREIGN KEY (course_id) REFERENCES courses(id)
) ENGINE=InnoDB;

CREATE TABLE academic_records (
  id INT AUTO_INCREMENT PRIMARY KEY,
  student_id INT NOT NULL,
  course_id INT NOT NULL,
  term VARCHAR(20) NOT NULL,
  marks DECIMAL(5,2) NOT NULL,
  max_marks DECIMAL(5,2) NOT NULL DEFAULT 100,
  grade VARCHAR(2) NOT NULL,
  UNIQUE KEY uq_record (student_id, course_id, term),
  FOREIGN KEY (student_id) REFERENCES students(id),
  FOREIGN KEY (course_id) REFERENCES courses(id)
) ENGINE=InnoDB;
