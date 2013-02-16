#!/usr/bin/python

# Converts incoming twist messages into outgoing drive messages
# Monitors for motor and encoder safety

import roslib; roslib.load_manifest('grizzly_node')
import rospy
import math

from geometry_msgs.msg import Twist
from roboteq_msgs.msg import Command
from roboteq_msgs.msg import Status,Feedback
from grizzly_msgs.msg import RawStatus
from std_msgs.msg import Bool

#motor indices
FR = 0
FL = 1
RR = 2
RL = 3

class MotionGenerator:
    def __init__(self):
        rospy.init_node('motion_generator')

        # Scale up angular rate to compensate for skid?
        #self.turn_scale = rospy.get_param('~turn_compensation', 1)

        #Encoder error watchdog parameters
        self.enc_watchdog_period = rospy.get_param('enc_watchdog_period',1/5.0) #default 10hz
        self.enc_error_time = rospy.get_param('enc_error_time',2)
        self.enc_error_thresh = rospy.get_param('enc_error_thresh',1500)


        # Vehicle Parameters
        self.width = rospy.get_param('~vehicle_width',1.01)
        self.gear_down = rospy.get_param('~gearing', 50.0)
        self.wheel_radius = rospy.get_param('~wheel_radius',0.333)
        self.max_rpm = rospy.get_param('~max_rpm',3500.0) #max command (1000) sent to roboteq attains this RPM value

               
        # 1 m/s equals how many RPMs at the wheel?
        rpm_scale = 1
        rpm_scale /= (2*math.pi*self.wheel_radius) #convert m/s to rotations at the wheel
        rpm_scale *= self.gear_down #convert rotations at the wheel to rotations at the motor
        rpm_scale *= 60 # seconds to mins

        #Convert rpms to roboteq input units (1000 units to get to max rpm)
        calc_scale = rpm_scale *  (1000.0/self.max_rpm)
        self.roboteq_scale = rospy.get_param('~roboteq_scale',calc_scale)
        self.mcu_watchdog_time = rospy.get_param('~mcu_watchdog_time',1)

        # Publishers & subscribers
        self.cmd_pub_fr = rospy.Publisher('motors/front_right/cmd', Command)
        self.cmd_pub_fl = rospy.Publisher('motors/front_left/cmd', Command)
        self.cmd_pub_rr = rospy.Publisher('motors/rear_right/cmd', Command)
        self.cmd_pub_rl = rospy.Publisher('motors/rear_left/cmd', Command)
        self.cmd_estop = rospy.Publisher('mcu/estop', Bool)

        #Serious faults where every motor should turn off
        self.serious_fault = [Status.FAULT_OVERHEAT, Status.FAULT_OVERVOLTAGE, Status.FAULT_SHORT_CIRCUIT, Status.FAULT_MOSFET_FAILURE]


        self.mcu_heartbeat_rxd = False 
        self.mcu_dead = False
        self.estop_status = RawStatus.ERROR_ESTOP_RESET

        
        self.mot_setting = [0,0,0,0]
        self.encreading = [0,0,0,0]
        self.enc_violations = [0,0,0,0] #track number of consecutive encoder error violations
        self.mot_heartbeat_rxd = [False, False, False, False]
        self.mot_node_dead = [True, True, True, True]


        #Assume there are no motor faults
        self.motor_fault = [False,False,False,False] #0-fr,1-fl,rr-2,rl-3

        rospy.Subscriber('motors/front_right/status',Status, self.fr_statCallback)
        rospy.Subscriber('motors/front_left/status',Status, self.fl_statCallback)
        rospy.Subscriber('motors/rear_right/status',Status, self.rr_statCallback)
        rospy.Subscriber('motors/rear_left/status',Status, self.rl_statCallback)
        
        rospy.Subscriber('motors/front_right/feedback',Feedback, self.fr_fbCallback)
        rospy.Subscriber('motors/front_left/feedback',Feedback, self.fl_fbCallback)
        rospy.Subscriber('motors/rear_right/feedback',Feedback, self.rr_fbCallback)
        rospy.Subscriber('motors/rear_left/feedback',Feedback, self.rl_fbCallback)

        rospy.Subscriber('mcu/status',RawStatus,self.mcu_statCallback)

        rospy.Timer(rospy.Duration(self.enc_watchdog_period), self.encoder_watchdog)
        rospy.Timer(rospy.Duration(self.mcu_watchdog_time), self.mcu_watchdog)
        rospy.Timer(rospy.Duration(2), self.mcu_watchdog)
        
        rospy.Subscriber("safe_cmd_vel", Twist, self.callback)

        rospy.spin()

    def callback(self, data):
        """ Receive Twist message, do kinematics, output.
        Right now, use same speed for both wheels on one side """
        cmd = Twist()
        right_speed = data.linear.x + data.angular.z*self.width/2;
        left_speed = data.linear.x - data.angular.z*self.width/2;

        # Scale to whatever Roboteq needs
        self.mot_setting[FR] = -right_speed * self.roboteq_scale
        self.mot_setting[FL] = left_speed * self.roboteq_scale
        self.mot_setting[RR] = -right_speed * self.roboteq_scale
        self.mot_setting[RL] = left_speed * self.roboteq_scale

        
        #Dont send the command if 
        #a) Motor is faulted
        #b) Motor node is dead
        #c) Mcu node is dead
        #d) Estop is not cleared and/or pre-charge is not completed)

        if ((True in self.motor_fault) or (True in self.mot_node_dead) or self.mcu_dead or (self.estop_status!=0)):
            #Turn off power to all motors, until fault is removed
            self.cmd_pub_fr.publish([int(0)])
            self.cmd_pub_fl.publish([int(0)])
            self.cmd_pub_rr.publish([int(0)])
            self.cmd_pub_rl.publish([int(0)])
        else:
            self.cmd_pub_fr.publish([int(self.mot_setting[FR])])
            self.cmd_pub_fl.publish([int(self.mot_setting[FL])])
            self.cmd_pub_rr.publish([int(self.mot_setting[RR])])
            self.cmd_pub_rl.publish([int(self.mot_setting[RL])])
            

    def mcu_statCallback(self,data):
        self.mcu_heartbeat_rxd = True
        self.estop_status = data.error 

    def mcu_watchdog(self, event):
        if (not self.mcu_heartbeat_rxd):
            self.mcu_dead = True
            rospy.logerr("MCU Comm is dead. Vehicle has been deactivated. Please reset systems")
        else:
            self.mcu_heartbeat_rxd = False
            self.mcu_dead = False


        #if pre charge status is activated, keep count
        if self.estop_status == RawStatus.ERROR_BRK_DET:
            self.pre_charge_timeout+=1
        else:
            self.pre_charge_timeout = 0

        #if precharge status is active for more than 4 seconds, fire estop. dont reset
        if self.pre_charge_timeout > (4/(self.mcu_watchdog_time)):
            rospy.logerr("Precharge malfunction. Estop activated. Please reboot all systems")
            cmd_estop.publish(True)

     
    #TODO: Combine into one callback? Callback data will need motor description
    def fr_statCallback(self, data):
        self.check_motor(data.fault,0)
        self.mot_heartbeat_rxd[FR] = True

    def fl_statCallback(self, data):
        self.check_motor(data.fault,1)
        self.mot_heartbeat_rxd[FL] = True

    def rr_statCallback(self, data):
        self.check_motor(data.fault,2)
        self.mot_heartbeat_rxd[RR] = True

    def rl_statCallback(self, data):
        self.check_motor(data.fault,3)
        self.mot_heartbeat_rxd[RL] = True

    def mot_watchdog(self, event): 
        if (False in self.mot_heartbeat_rxd): #its been 0.5 seconds since we've received data from the motor controllers, kill all motion
            f_index = self.mot_heartbeat_rxd.index(False )
            self.mot_node_dead[f_index] = True
            rospy.logerr(self.get_motor_string(f_index) + " motor controller node is dead. Vehicle has been deactivated. Please reset systems")
        else:
            self.mot_heartbeat_rxd = [False, False, False, False]
            self.mot_node_dead = [False, False, False, False]
            

    def fr_fbCallback(self, data):
        self.encreading[FR] = data.encoder_rpm[0]

    def fl_fbCallback(self, data):
        self.encreading[FL] = data.encoder_rpm[0]

    def rr_fbCallback(self, data):
        self.encreading[RR] = data.encoder_rpm[0]

    def rl_fbCallback(self, data):
        self.encreading[RL] = data.encoder_rpm[0]

    #TODO: Log at X frequency, instead of streaming the logs out
    def check_motor(self, status, motor_num): 
        if (status in self.serious_fault): # there is a serious motor fault, turn flag on and send user error 
            rospy.logerr(self.get_motor_string (motor_num)+ " Motor Error:" + str(status))
            self.motor_fault[motor_num] = True
        elif (not self.motor_fault[motor_num]): #there is no fault, and the fault flag is on i.e. motor just came out of a fault
            self.motor_fault[motor_num] = False

    def encoder_watchdog(self, event):
        for i in range(0,4):
            error = math.fabs(self.mot_setting[i]*(self.max_rpm/1000.0) - self.encreading[i])
            if (error > self.enc_error_thresh):
                self.enc_violations[i]+=1
            else:
                self.enc_violations[i]=0

            if (self.enc_violations[i] * self.enc_watchdog_period >= self.enc_error_time):
                rospy.logerr("Encoder Fault on the " + self.get_motor_string(i) + " Motor")
                self.motor_fault[i] = True
            else:
                self.motor_fault[i] = False

    def get_motor_string(self, mot):
        return {
            FR: 'Front Right',
            FL: 'Front Left',
            RR: 'Rear Right',
            RL: 'Rear Left' 
        }.get(mot,'Unknown')


if __name__ == "__main__": 
    MotionGenerator()
